"""
GET /api/v1/dashboard 의 has_anomaly 배지 통합테스트.

patients/overview와 달리 대시보드는 "특정 날짜(record_date) 화면"이라, 여기서
has_anomaly는 patient_daily_analytics의 "가장 최근" 캐시가 아니라 조회 중인
그 날짜(record_date)와 정확히 일치하는 캐시만 반영해야 함(routes/dashboard.py
_ 캐시 조회 참고). 이 차이를 명시적으로 검증한다.
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
            (patient_id, record_date, trend_json, anomaly_json, correlation_json, eda_json,
             has_anomaly, anomaly_attrs)
        VALUES
            (:pid, :rdate, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, :ha, ARRAY[]::text[])
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
