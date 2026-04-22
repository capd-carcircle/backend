"""
patients.py — 의사용 환자 관리 API

환자 목록: patient_registrations(status=completed)로 주치의-환자 관계 필터링
"""
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.record import DailyRecord, RecordStatus
from app.models.registration import PatientRegistration, RegistrationStatus
from app.models.user import User, UserRole
from pydantic import BaseModel

router = APIRouter(prefix="/patients", tags=["환자 관리"])


# ── 스키마 ──────────────────────────────────────────────────

class PatientInfo(BaseModel):
    id:           int
    name:         str
    phone_number: str

class PatientRecordRow(BaseModel):
    record_id:    int
    record_date:  str
    submitted_at: Optional[str]
    status:       str

class PatientRecordsResponse(BaseModel):
    patient_id:   int
    patient_name: str
    records:      List[PatientRecordRow]


# ── 헬퍼 ──────────────────────────────────────────────────

def _require_doctor(current_user: User) -> None:
    if current_user.role != UserRole.doctor:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="의사만 접근할 수 있습니다.",
        )


# ── 엔드포인트 ────────────────────────────────────────────

@router.get(
    "",
    response_model=List[PatientInfo],
    summary="나의 환자 목록",
    description="patient_registrations(completed)로 연결된 담당 환자만 반환.",
)
def list_patients(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[PatientInfo]:
    _require_doctor(current_user)

    # patient_registrations.completed 기준으로 담당 환자 조회
    patient_ids = (
        db.query(PatientRegistration.user_id)
        .filter(
            PatientRegistration.doctor_id == current_user.id,
            PatientRegistration.status == RegistrationStatus.completed,
            PatientRegistration.user_id.isnot(None),
        )
        .subquery()
    )

    patients = (
        db.query(User)
        .filter(User.id.in_(patient_ids), User.is_active == True)
        .order_by(User.name)
        .all()
    )
    return [PatientInfo(id=p.id, name=p.name, phone_number=p.phone_number) for p in patients]


@router.get(
    "/{patient_id}/records",
    response_model=PatientRecordsResponse,
    summary="환자별 과거 기록 목록",
)
def list_patient_records(
    patient_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PatientRecordsResponse:
    _require_doctor(current_user)

    # 담당 환자인지 확인
    reg = db.query(PatientRegistration).filter(
        PatientRegistration.doctor_id == current_user.id,
        PatientRegistration.user_id == patient_id,
        PatientRegistration.status == RegistrationStatus.completed,
    ).first()
    if not reg:
        raise HTTPException(status_code=404, detail="담당 환자를 찾을 수 없습니다.")

    patient = db.query(User).filter(User.id == patient_id, User.is_active == True).first()
    if not patient:
        raise HTTPException(status_code=404, detail="환자를 찾을 수 없습니다.")

    records = (
        db.query(DailyRecord)
        .filter(DailyRecord.patient_id == patient_id)
        .order_by(desc(DailyRecord.record_date))
        .all()
    )

    rows = [
        PatientRecordRow(
            record_id    = r.id,
            record_date  = r.record_date.isoformat(),
            submitted_at = r.submitted_at.isoformat() if r.submitted_at else None,
            status       = r.status.value,
        )
        for r in records
    ]

    return PatientRecordsResponse(
        patient_id   = patient.id,
        patient_name = patient.name,
        records      = rows,
    )
