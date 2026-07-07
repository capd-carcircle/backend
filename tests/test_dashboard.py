"""
GET /api/v1/dashboard 의 has_anomaly 배지 통합테스트.

patients/overview와 달리 대시보드는 "특정 날짜(record_date) 화면"이라, 과거
날짜를 조회할 때는 patient_daily_analytics의 "가장 최근" 캐시가 아니라 조회
중인 그 날짜(record_date)와 정확히 일치하는 캐시만 반영해야 함(routes/dashboard.py
캐시 조회 참고). 이 차이를 명시적으로 검증한다.

단, 실제 달력 기준 "오늘"을 조회할 때는 예외 — Airflow가 계산하는 캐시의
record_date는 그 환자의 마지막 제출/승인 기록 기준이라, 오늘 새 기록을 안 낸
환자는 캐시 날짜가 실제 오늘과 달라 정확 일치로는 배지가 못 뜨는 문제가 있었음
(2026-07-07 수정). 그래서 target_date == date.today()일 때만 patients/overview와
동일하게 "가장 최근" 캐시를 사용하도록 완화함 — 이것도 별도 테스트로 검증.
"""
from datetime import date

from sqlalchemy import text

from app.models.record import DailyRecord, RecordStatus, RiskLevel

TARGET_DATE = date(2026, 6, 10)


def _insert_record(db_session, patient_id, record_date):
    record = DailyRecord(
        patient_id=patient_id, record_date=record_date,
        status=RecordStatus.submitted, risk_level=RiskLevel.normal,
    )
    db_session.add(record)
    db_session.commit()
    return record


def _insert_gold_cache(db_session, patient_id, record_date, has_anomaly: bool):
    db_session.execute(text("""
        INSERT INTO patient_daily_analytics
            (patient_id, record_date, window_days, trend_json, anomaly_json, correlation_json, eda_json,
             has_anomaly, anomaly_attrs)
        VALUES
            (:pid, :rdate, 30, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, :ha, ARRAY[]::text[])
    """), {"pid": patient_id, "rdate": record_date, "ha": has_anomaly})
    db_session.commit()


def test_dashboard_has_anomaly_none_without_cache(
    client, db_session, assigned_patient, doctor_user, make_auth_headers,
):
    _insert_record(db_session, assigned_patient.id, TARGET_DATE)

    res = client.get(
        f"/api/v1/dashboard?record_date={TARGET_DATE.isoformat()}",
        headers=make_auth_headers(doctor_user),
    )
    assert res.status_code == 200
    rows = res.json()["records"]
    row = next(r for r in rows if r["patient_id"] == assigned_patient.id)
    assert row["has_anomaly"] is None


def test_dashboard_has_anomaly_reflects_exact_date_cache(
    client, db_session, assigned_patient, doctor_user, make_auth_headers,
):
    _insert_record(db_session, assigned_patient.id, TARGET_DATE)
    _insert_gold_cache(db_session, assigned_patient.id, TARGET_DATE, has_anomaly=True)

    res = client.get(
        f"/api/v1/dashboard?record_date={TARGET_DATE.isoformat()}",
        headers=make_auth_headers(doctor_user),
    )
    assert res.status_code == 200
    row = next(r for r in res.json()["records"] if r["patient_id"] == assigned_patient.id)
    assert row["has_anomaly"] is True


def test_dashboard_ignores_cache_from_a_different_date(
    client, db_session, assigned_patient, doctor_user, make_auth_headers,
):
    """
    patients/overview는 "가장 최근" 캐시를 쓰지만 dashboard는 조회 중인 날짜와
    정확히 일치하는 캐시만 씀 -- 다른 날짜의 캐시는 무시돼야 함.
    """
    other_date = date(2026, 6, 5)
    _insert_record(db_session, assigned_patient.id, TARGET_DATE)
    _insert_gold_cache(db_session, assigned_patient.id, other_date, has_anomaly=True)

    res = client.get(
        f"/api/v1/dashboard?record_date={TARGET_DATE.isoformat()}",
        headers=make_auth_headers(doctor_user),
    )
    assert res.status_code == 200
    row = next(r for r in res.json()["records"] if r["patient_id"] == assigned_patient.id)
    assert row["has_anomaly"] is None


def test_dashboard_today_uses_most_recent_cache_regardless_of_date(
    db_session, assigned_patient, doctor_user, make_auth_headers, client,
):
    """
    실제 '오늘'을 조회할 때는(record_date 파라미터 생략) patients/overview와 동일하게
    "가장 최근" 캐시를 반영해야 함 — 환자가 오늘 새 기록을 안 냈어도(캐시 날짜가
    오늘과 다르더라도) 마지막으로 계산된 이상치 여부가 그대로 보여야 함.
    """
    today = date.today()
    earlier_date = date(2020, 1, 1)  # today와 절대 안 겹치는 먼 과거

    _insert_record(db_session, assigned_patient.id, today)
    _insert_gold_cache(db_session, assigned_patient.id, earlier_date, has_anomaly=True)

    res = client.get(
        "/api/v1/dashboard",
        headers=make_auth_headers(doctor_user),
    )
    assert res.status_code == 200
    row = next(r for r in res.json()["records"] if r["patient_id"] == assigned_patient.id)
    assert row["has_anomaly"] is True
