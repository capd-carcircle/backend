"""
환자 기록 draft -> submit -> 의사 승인/승인취소 흐름 통합테스트.

설문 완료 -> AI 위험도 산출(risk_level) 파이프라인은 ai 서버 HTTP 호출이 껴 있어
이 테스트 범위 밖 -- 승인 테스트에서는 risk_level을 DB에 직접 채워 넣어
"설문까지 끝난 상태"를 시뮬레이션한다.
"""
from datetime import date, datetime, timezone

from app.models.patient_assignment import PatientDoctorAssignment
from app.models.record import DailyRecord, RecordStatus, RiskLevel


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


def test_delete_allowed_before_review_blocked_after(
    client, db_session, assigned_patient, doctor_user, make_auth_headers,
):
    """draft/submitted는 환자 본인이 삭제 가능, reviewed(승인완료)는 삭제 불가(409)."""
    patient_headers = make_auth_headers(assigned_patient)
    doctor_headers = make_auth_headers(doctor_user)

    draft_record = DailyRecord(
        patient_id=assigned_patient.id, record_date=date(2026, 6, 2),
        status=RecordStatus.draft,
    )
    db_session.add(draft_record)
    db_session.commit()

    # draft 상태 -> 삭제 허용
    del_res = client.delete(f"/api/v1/records/{draft_record.id}", headers=patient_headers)
    assert del_res.status_code == 204

    reviewed_record = DailyRecord(
        patient_id=assigned_patient.id, record_date=date(2026, 6, 3),
        status=RecordStatus.reviewed, risk_level=RiskLevel.normal,
    )
    db_session.add(reviewed_record)
    db_session.commit()

    # reviewed 상태 -> 삭제 차단
    blocked_res = client.delete(f"/api/v1/records/{reviewed_record.id}", headers=patient_headers)
    assert blocked_res.status_code == 409

    # 의사 계정으로 삭제 시도 -- _require_patient에서 먼저 걸려 403(역할 자체가 다른 경우)
    doctor_attempt = client.delete(f"/api/v1/records/{reviewed_record.id}", headers=doctor_headers)
    assert doctor_attempt.status_code == 403


def test_bulk_approve_records(
    client, db_session, assigned_patient, doctor_user, make_auth_headers,
):
    """일괄승인 — risk_level 있는 현재 담당 환자 기록만 승인되고, 나머지는 조용히 skip."""
    ready_record = DailyRecord(
        patient_id=assigned_patient.id, record_date=date(2026, 6, 4),
        status=RecordStatus.submitted, risk_level=RiskLevel.normal,
    )
    not_ready_record = DailyRecord(  # risk_level 없음 -> 일괄승인 대상에서 제외돼야 함
        patient_id=assigned_patient.id, record_date=date(2026, 6, 5),
        status=RecordStatus.submitted,
    )
    db_session.add_all([ready_record, not_ready_record])
    db_session.commit()

    res = client.post(
        "/api/v1/records/bulk-approve",
        json={"record_ids": [ready_record.id, not_ready_record.id]},
        headers=make_auth_headers(doctor_user),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["approved"] == [ready_record.id]
    assert body["total"] == 1

    db_session.refresh(ready_record)
    db_session.refresh(not_ready_record)
    assert ready_record.status == RecordStatus.reviewed
    assert ready_record.approved_by == doctor_user.id
    assert not_ready_record.status == RecordStatus.submitted  # 미변경


def test_past_doctor_cannot_access_records_after_assignment_ended(
    client, db_session, doctor_user, patient_user, make_auth_headers,
):
    """담당 기간이 끝난 의사는 종료일 이전 기록만 보고, 종료 이후 기록은 403."""
    started_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
    ended_at = datetime(2026, 5, 15, tzinfo=timezone.utc)
    db_session.add(PatientDoctorAssignment(
        patient_id=patient_user.id, doctor_id=doctor_user.id,
        started_at=started_at, ended_at=ended_at,
    ))

    within_period = DailyRecord(
        patient_id=patient_user.id, record_date=date(2026, 5, 10),
        status=RecordStatus.submitted, risk_level=RiskLevel.normal,
    )
    after_period = DailyRecord(
        patient_id=patient_user.id, record_date=date(2026, 5, 20),
        status=RecordStatus.submitted, risk_level=RiskLevel.normal,
    )
    db_session.add_all([within_period, after_period])
    db_session.commit()

    headers = make_auth_headers(doctor_user)

    ok_res = client.get(f"/api/v1/records/{within_period.id}/detail", headers=headers)
    assert ok_res.status_code == 200

    blocked_res = client.get(f"/api/v1/records/{after_period.id}/detail", headers=headers)
    assert blocked_res.status_code == 403
