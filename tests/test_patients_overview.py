"""
GET /api/v1/patients/overview 의 has_anomaly 배지 통합테스트.

배지는 patient_daily_analytics(Gold) 캐시가 있는 환자에서만 뜬다 — 의사가
"분석 리포트"를 한 번도 열어본 적 없는 환자는 캐시 자체가 없어 has_anomaly=None
(정상 동작, CLAUDE.md "알려진 제한사항" 1번 참고).
"""
from datetime import date

from sqlalchemy import text


def test_overview_has_anomaly_none_without_cache(client, assigned_patient, doctor_user, make_auth_headers):
    res = client.get("/api/v1/patients/overview?scope=current", headers=make_auth_headers(doctor_user))
    assert res.status_code == 200

    row = next(r for r in res.json() if r["id"] == assigned_patient.id)
    assert row["has_anomaly"] is None
    assert row["anomaly_record_date"] is None


def test_overview_has_anomaly_reflects_latest_gold_cache(
    client, db_session, assigned_patient, doctor_user, make_auth_headers,
):
    older_date = date(2026, 5, 1)
    latest_date = date(2026, 6, 1)

    # 과거 계산일(이상 없음)
    db_session.execute(text("""
        INSERT INTO patient_daily_analytics
            (patient_id, record_date, window_days, trend_json, anomaly_json, correlation_json, eda_json,
             has_anomaly, anomaly_attrs)
        VALUES
            (:pid, :rdate, 30, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, FALSE, ARRAY[]::text[])
    """), {"pid": assigned_patient.id, "rdate": older_date})

    # 가장 최근 계산일(이상 있음) -- 목록은 이 값만 반영해야 함
    db_session.execute(text("""
        INSERT INTO patient_daily_analytics
            (patient_id, record_date, window_days, trend_json, anomaly_json, correlation_json, eda_json,
             has_anomaly, anomaly_attrs)
        VALUES
            (:pid, :rdate, 30, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, TRUE, ARRAY['body_weight_kg'])
    """), {"pid": assigned_patient.id, "rdate": latest_date})
    db_session.commit()

    res = client.get("/api/v1/patients/overview?scope=current", headers=make_auth_headers(doctor_user))
    assert res.status_code == 200

    row = next(r for r in res.json() if r["id"] == assigned_patient.id)
    assert row["has_anomaly"] is True
    assert row["anomaly_record_date"] == latest_date.isoformat()
