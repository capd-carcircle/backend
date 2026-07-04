"""
GET /api/v1/analytics/patients/{id} 통합테스트 (실제 Postgres 경유).

- 순수 함수(analytics_engine.run_all_tasks) 기대값과 API 응답이 일치하는지
- 같은 window로 재호출하면 Gold 캐시(source=cache)를 타는지
- 담당 이력 없는 의사는 403인지

기록은 API(submit 흐름)를 거치지 않고 DB에 직접 넣는다 -- 설문/AI 파이프라인은
이 테스트 범위 밖이라, 이미 submitted 상태인 기록이 여러 날짜치 있는 상황만
필요하기 때문.
"""
from datetime import date, timedelta

import pytest

from app.models.record import DailyRecord, ExchangeRecord, RecordStatus, RiskLevel
from app.services.analytics_engine import build_daily_model_row, run_all_tasks

from synth import gen_synthetic_series

WINDOW = 7
N_DAYS = 10
START = date(2026, 5, 1)


def _insert_submitted_record(db_session, patient_id, day, daily_data, exchanges):
    record = DailyRecord(
        patient_id=patient_id,
        record_date=day,
        turbid_peritoneal=bool(daily_data.get("turbid_peritoneal")),
        weight=daily_data.get("weight"),
        blood_pressure=daily_data.get("blood_pressure"),
        urine_count=daily_data.get("urine_count"),
        total_ultrafiltration=daily_data.get("total_ultrafiltration"),
        fasting_blood_glucose=daily_data.get("fasting_blood_glucose"),
        memo=daily_data.get("note"),
        status=RecordStatus.submitted,
        risk_level=RiskLevel.normal,
    )
    db_session.add(record)
    db_session.flush()
    for ex in exchanges:
        db_session.add(ExchangeRecord(
            daily_record_id=record.id,
            session_number=ex["session_number"],
            exchange_time=ex.get("exchange_time"),
            drainage_volume=ex.get("drainage_volume"),
            infusion_concentration=ex.get("infusion_concentration"),
            infusion_weight=ex.get("infusion_weight"),
            ultrafiltration=ex.get("ultrafiltration"),
        ))
    db_session.commit()
    return record


def _seed_series(db_session, patient_id):
    series = gen_synthetic_series(seed=7, n_days=N_DAYS, start=START)
    for i, (daily_data, exchanges) in enumerate(series):
        _insert_submitted_record(db_session, patient_id, START + timedelta(days=i), daily_data, exchanges)
    return series


def _expected_result(series, window):
    """
    엔드포인트가 실제로 쓰는 것과 동일한 부분집합으로 기대값을 계산한다.
    엔드포인트는 record_date DESC로 최신 (window+1)건만 조회하므로,
    n_days > window+1이면 가장 오래된 기록 일부는 애초에 안 쓰인다.
    """
    recent = series[-(window + 1):]  # 과거->최신, 최근 (window+1)일
    rows_oldest_first = [build_daily_model_row(d, e) for d, e in recent]
    rows_newest_first = list(reversed(rows_oldest_first))
    today_row, *historical_rows = rows_newest_first
    return today_row, historical_rows, run_all_tasks(today_row, historical_rows, window=window)


def test_analytics_endpoint_matches_pure_function_and_caches(
    client, db_session, assigned_patient, doctor_user, make_auth_headers,
):
    series = _seed_series(db_session, assigned_patient.id)
    _, historical_rows, expected = _expected_result(series, WINDOW)
    headers = make_auth_headers(doctor_user)

    res = client.get(
        f"/api/v1/analytics/patients/{assigned_patient.id}?window={WINDOW}", headers=headers
    )
    assert res.status_code == 200
    body = res.json()

    assert body["source"] == "on_demand"
    assert body["window_days"] == len(historical_rows)
    assert body["has_anomaly"] == expected["has_anomaly"]
    assert set(body["trend_analysis"]["results"].keys()) == set(expected["trend_analysis"]["results"].keys())

    # 핵심 지표 하나를 골라 순수 함수 기대값과 근사 일치 확인
    # (Postgres Numeric -> float 왕복을 거치므로 완전 동일 대신 근사 비교 —
    #  바이트 단위 일치는 DB 없는 test_ai_parity.py가 이미 보증함)
    expected_weight = expected["trend_analysis"]["results"]["body_weight_kg"]["today_value"]
    actual_weight = body["trend_analysis"]["results"]["body_weight_kg"]["today_value"]
    assert actual_weight == pytest.approx(expected_weight, abs=0.05)

    # 같은 window로 재호출 -> Gold 캐시 적중
    res_cached = client.get(
        f"/api/v1/analytics/patients/{assigned_patient.id}?window={WINDOW}", headers=headers
    )
    assert res_cached.status_code == 200
    assert res_cached.json()["source"] == "cache"


def test_analytics_endpoint_window_switch_invalidates_cache(
    client, db_session, assigned_patient, doctor_user, make_auth_headers,
):
    """
    CLAUDE.md "알려진 제한사항" 2번 재현: patient_daily_analytics는 (patient_id,
    record_date)당 1행만 저장하고 그 안에 마지막으로 계산된 window 하나만 들어있어서,
    7<->30<->90 전환할 때마다 서로의 캐시를 무효화시키고 매번 재계산됨(의도된 동작,
    버그 아님). 같은 window를 다시 요청할 때만 캐시 적중.
    """
    n_days = 35  # window=7/30 둘 다 실제로 다른 historical 개수를 쓰게 충분히 확보
    series = gen_synthetic_series(seed=11, n_days=n_days, start=START)
    for i, (daily_data, exchanges) in enumerate(series):
        _insert_submitted_record(
            db_session, assigned_patient.id, START + timedelta(days=i), daily_data, exchanges
        )

    headers = make_auth_headers(doctor_user)
    url = f"/api/v1/analytics/patients/{assigned_patient.id}"

    res7 = client.get(f"{url}?window=7", headers=headers)
    assert res7.status_code == 200
    assert res7.json()["source"] == "on_demand"
    assert res7.json()["window_days"] == 7

    # window을 30으로 바꾸면 캐시에 저장된 window_days(7)와 달라 재계산됨
    res30 = client.get(f"{url}?window=30", headers=headers)
    assert res30.status_code == 200
    assert res30.json()["source"] == "on_demand"
    assert res30.json()["window_days"] == 30

    # 같은 window(30)로 재호출 -> 이번엔 캐시 적중
    res30_again = client.get(f"{url}?window=30", headers=headers)
    assert res30_again.json()["source"] == "cache"

    # 다시 7로 돌아가면 캐시(30)와 또 달라 재계산 -- 알려진 제한사항 재현
    res7_again = client.get(f"{url}?window=7", headers=headers)
    assert res7_again.json()["source"] == "on_demand"
    assert res7_again.json()["window_days"] == 7


def test_analytics_endpoint_403_for_unassigned_doctor(
    client, patient_user, doctor_user, make_auth_headers,
):
    """담당 이력이 전혀 없는 의사는 접근 불가."""
    res = client.get(
        f"/api/v1/analytics/patients/{patient_user.id}?window={WINDOW}",
        headers=make_auth_headers(doctor_user),
    )
    assert res.status_code == 403


def test_analytics_endpoint_404_without_records(
    client, assigned_patient, doctor_user, make_auth_headers,
):
    """제출/승인된 기록이 하나도 없으면 404."""
    res = client.get(
        f"/api/v1/analytics/patients/{assigned_patient.id}?window={WINDOW}",
        headers=make_auth_headers(doctor_user),
    )
    assert res.status_code == 404
