"""
환자 기록 draft -> submit -> 의사 승인/승인취소 흐름 통합테스트.

설문 완료 -> AI 위험도 산출(risk_level) 파이프라인은 ai 서버 HTTP 호출이 껴 있어
이 테스트 범위 밖 -- 승인 테스트에서는 risk_level을 DB에 직접 채워 넣어
"설문까지 끝난 상태"를 시뮬레이션한다.
"""
from datetime import date

from app.models.record import DailyRecord, RiskLevel


def test_submit_blocked_without_doctor(client, patient_user, make_auth_headers):
    """담당 의사가 없는 환자는 기록 생성 자체가 차단된다."""
    res = client.post("/api/v1/records", json={
        "record_date": str(date(2026, 6, 1)),
        "weight": 60.0,
    }, headers=make_auth_headers(patient_user))
    assert res.status_code == 403


def test_draft_submit_approve_revert_flow(
    client, db_session, assigned_patient, doctor_user, make_auth_headers,
):
    patient_headers = make_auth_headers(assigned_patient)
    doctor_headers = make_auth_headers(doctor_user)

    # 1) draft 생성
    create_res = client.post("/api/v1/records", json={
        "record_date": str(date(2026, 6, 1)),
        "weight": 61.5,
        "blood_pressure": "130/85",
        "total_ultrafiltration": 450.0,
        "exchange_records": [
            {
                "session_number": 1,
                "drainage_volume": 2050.0,
                "infusion_weight": 2000.0,
                "infusion_concentration": 1.5,
            },
        ],
    }, headers=patient_headers)
    assert create_res.status_code == 201
    record_id = create_res.json()["id"]
    assert create_res.json()["status"] == "draft"

    # 2) draft 상태에서만 수정 가능
    patch_res = client.patch(
        f"/api/v1/records/{record_id}", json={"weight": 62.0}, headers=patient_headers
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["weight"] == 62.0

    # 3) 최종 제출 (draft -> submitted)
    submit_res = client.post(f"/api/v1/records/{record_id}/submit", headers=patient_headers)
    assert submit_res.status_code == 200
    assert submit_res.json()["status"] == "submitted"

    # 4) 제출 후에는 수정 불가
    patch_after_submit = client.patch(
        f"/api/v1/records/{record_id}", json={"weight": 70.0}, headers=patient_headers
    )
    assert patch_after_submit.status_code == 409

    # 5) risk_level 없으면 의사 승인 불가(설문 미완료로 간주)
    approve_before = client.patch(f"/api/v1/records/{record_id}/approve", headers=doctor_headers)
    assert approve_before.status_code == 400

    # 설문/AI 파이프라인 없이 risk_level만 직접 채워서 "승인 가능 상태" 시뮬레이션
    record = db_session.query(DailyRecord).filter(DailyRecord.id == record_id).first()
    record.risk_level = RiskLevel.normal
    db_session.commit()

    # 6) 승인
    approve_res = client.patch(f"/api/v1/records/{record_id}/approve", headers=doctor_headers)
    assert approve_res.status_code == 200
    assert approve_res.json()["success"] is True

    # 7) 이미 승인된 기록 재승인 시도 -> 충돌
    approve_again = client.patch(f"/api/v1/records/{record_id}/approve", headers=doctor_headers)
    assert approve_again.status_code == 409

    # 8) 승인 취소 -> reviewed에서 submitted로
    revert_res = client.patch(f"/api/v1/records/{record_id}/revert", headers=doctor_headers)
    assert revert_res.status_code == 200

    db_session.refresh(record)
    assert record.status.value == "submitted"
    assert record.approved_by is None
