"""
GET /api/v1/dashboard 의 has_anomaly 배지 통합테스트.

이상치는 "그 날짜의 기록이 어땠는가"가 아니라 "이 환자가 지금 이상 소견이
있는가"를 나타내는 현재-상태 개념으로 정리함(2026-07-08). 그래서:
- 실제 달력 기준 "오늘"을 조회할 때만 patients/overview와 동일하게 환자별
  "가장 최근" Gold 캐시를 반영(record_date가 오늘과 달라도 상관없음 — 오늘
  새 기록을 안 낸 환자도 마지막으로 알려진 이상치 상태를 그대로 보여줌).
- 과거 날짜를 캘린더로 조회할 때는 캐시가 있든 없든 항상 None(배지 없음) —
  과거 기록 열람 화면에 "현재" 상태를 갖다 붙이면 그 날짜에 문제가 있었다는
  것처럼 오인될 수 있어서 아예 조회하지 않음.

(참고: 2026-07-07엔 과거 날짜도 "정확히 그 날짜와 일치하는 캐시"만 반영하는
절충안이었으나, 하루만 지나도 어제 화면에서 배지가 사라지는 혼란만 있고
실익이 없어 2026-07-08에 "과거 날짜는 항상 None"으로 단순화함.)
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


def test_dashboard_past_date_never_shows_anomaly_badge(
    client, db_session, assigned_patient, doctor_user, make_auth_headers,
):
    """
    과거 날짜 조회는 이상치가 "현재 상태" 개념이라 애초에 캐시를 조회하지 않음 —
    그 날짜와 정확히 일치하는 캐시가 있든, 다른 날짜의 캐시만 있든 항상 None.
    """
    other_date = date(2026, 6, 5)
    _insert_record(db_session, assigned_patient.id, TARGET_DATE)
    _insert_gold_cache(db_session, assigned_patient.id, TARGET_DATE, has_anomaly=True)
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
