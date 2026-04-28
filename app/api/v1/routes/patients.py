"""
patients.py — 의사용 환자 관리 API

환자 목록: patient_registrations(status=completed)로 주치의-환자 관계 필터링
"""
from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.hospital import Hospital
from app.models.patient_note import PatientNote
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

class PatientOverview(BaseModel):
    id:                   int
    name:                 str
    phone_number:         str
    total_records:        int
    last_record_date:     Optional[str]
    last_submitted_at:    Optional[str]
    latest_risk_level:    Optional[str]
    days_since_last_record: Optional[int]


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
    description="patient_registrations(completed) 또는 users.doctor_id 기준으로 담당 환자 반환 (시드 데이터 호환).",
)
def list_patients(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[PatientInfo]:
    _require_doctor(current_user)

    # patient_registrations.completed로 연결된 환자 ID
    reg_ids = (
        db.query(PatientRegistration.user_id)
        .filter(
            PatientRegistration.doctor_id == current_user.id,
            PatientRegistration.status == RegistrationStatus.completed,
            PatientRegistration.user_id.isnot(None),
        )
        .subquery()
    )

    # OR: users.doctor_id로 직접 연결된 환자 (시드 데이터 등)
    from sqlalchemy import or_
    patients = (
        db.query(User)
        .filter(
            User.role == UserRole.patient,
            User.is_active == True,
            or_(
                User.id.in_(reg_ids),
                User.doctor_id == current_user.id,
            ),
        )
        .order_by(User.name)
        .all()
    )
    return [PatientInfo(id=p.id, name=p.name, phone_number=p.phone_number) for p in patients]


@router.get(
    "/overview",
    response_model=List[PatientOverview],
    summary="환자별 요약 정보 (전체 기록 수, 마지막 제출일, 최근 위험도)",
)
def list_patients_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[PatientOverview]:
    _require_doctor(current_user)

    from sqlalchemy import or_, func
    reg_ids = (
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
        .filter(
            User.role == UserRole.patient,
            User.is_active == True,
            or_(User.id.in_(reg_ids), User.doctor_id == current_user.id),
        )
        .order_by(User.name)
        .all()
    )

    today = datetime.now(timezone.utc).date()
    result = []
    for p in patients:
        records = (
            db.query(DailyRecord)
            .filter(DailyRecord.patient_id == p.id)
            .order_by(desc(DailyRecord.record_date))
            .all()
        )
        submitted = [r for r in records if r.status.value in ("submitted", "reviewed")]
        last_rec  = records[0] if records else None
        last_sub  = submitted[0] if submitted else None

        days_since = None
        if last_rec:
            days_since = (today - last_rec.record_date).days

        result.append(PatientOverview(
            id                    = p.id,
            name                  = p.name,
            phone_number          = p.phone_number,
            total_records         = len(records),
            last_record_date      = last_rec.record_date.isoformat() if last_rec else None,
            last_submitted_at     = last_sub.submitted_at.isoformat() if last_sub and last_sub.submitted_at else None,
            latest_risk_level     = last_sub.risk_level.value if last_sub and last_sub.risk_level else None,
            days_since_last_record= days_since,
        ))
    return result


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

    # 담당 환자인지 확인 — registrations 또는 doctor_id 둘 중 하나라도 있으면 허용
    from sqlalchemy import or_
    patient_check = db.query(User).filter(
        User.id == patient_id,
        User.role == UserRole.patient,
        User.is_active == True,
        or_(
            User.doctor_id == current_user.id,
            User.id.in_(
                db.query(PatientRegistration.user_id).filter(
                    PatientRegistration.doctor_id == current_user.id,
                    PatientRegistration.status == RegistrationStatus.completed,
                    PatientRegistration.user_id.isnot(None),
                )
            ),
        ),
    ).first()
    if not patient_check:
        raise HTTPException(status_code=404, detail="담당 환자를 찾을 수 없습니다.")

    patient = patient_check

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


# ── 환자 상세 프로필 (의사용) ──────────────────────────────────

class PatientDetailProfile(BaseModel):
    id:           int
    name:         str
    phone_number: str
    birth_date:   Optional[str]
    hospital_name: Optional[str]
    doctor_name:  Optional[str]
    self_memo:    Optional[str]   # 환자 자기 메모 (읽기 전용)
    joined_at:    Optional[str]   # 가입일


@router.get(
    "/{patient_id}/profile",
    response_model=PatientDetailProfile,
    summary="환자 상세 프로필 (의사용)",
)
def get_patient_profile(
    patient_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PatientDetailProfile:
    _require_doctor(current_user)

    from sqlalchemy import or_
    patient = db.query(User).filter(
        User.id == patient_id,
        User.role == UserRole.patient,
        User.is_active == True,
        or_(
            User.doctor_id == current_user.id,
            User.id.in_(
                db.query(PatientRegistration.user_id).filter(
                    PatientRegistration.doctor_id == current_user.id,
                    PatientRegistration.status == RegistrationStatus.completed,
                    PatientRegistration.user_id.isnot(None),
                )
            ),
        ),
    ).first()
    if not patient:
        raise HTTPException(status_code=404, detail="담당 환자를 찾을 수 없습니다.")

    hospital = db.query(Hospital).filter_by(id=patient.hospital_id).first() if patient.hospital_id else None
    doctor   = db.query(User).filter_by(id=patient.doctor_id).first() if patient.doctor_id else None

    return PatientDetailProfile(
        id           = patient.id,
        name         = patient.name,
        phone_number = patient.phone_number,
        birth_date   = patient.birth_date,
        hospital_name= hospital.name if hospital else None,
        doctor_name  = doctor.name if doctor else None,
        self_memo    = patient.self_memo,
        joined_at    = patient.created_at.isoformat() if patient.created_at else None,
    )


# ── 의사 메모 (단일 메모, 의사 전용) ──────────────────────────

class PatientNoteResponse(BaseModel):
    content: Optional[str]
    updated_at: Optional[str]


class PatientNoteUpsert(BaseModel):
    content: str


@router.get(
    "/{patient_id}/note",
    response_model=PatientNoteResponse,
    summary="의사 메모 조회",
)
def get_patient_note(
    patient_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PatientNoteResponse:
    _require_doctor(current_user)
    note = db.query(PatientNote).filter_by(
        doctor_id=current_user.id, patient_id=patient_id
    ).first()
    if not note:
        return PatientNoteResponse(content=None, updated_at=None)
    return PatientNoteResponse(
        content=note.content,
        updated_at=note.updated_at.isoformat(),
    )


@router.put(
    "/{patient_id}/note",
    response_model=PatientNoteResponse,
    summary="의사 메모 저장 (upsert)",
)
def upsert_patient_note(
    patient_id: int,
    payload: PatientNoteUpsert,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PatientNoteResponse:
    _require_doctor(current_user)

    note = db.query(PatientNote).filter_by(
        doctor_id=current_user.id, patient_id=patient_id
    ).first()

    if note:
        note.content = payload.content
    else:
        note = PatientNote(
            doctor_id=current_user.id,
            patient_id=patient_id,
            content=payload.content,
        )
        db.add(note)

    db.commit()
    db.refresh(note)
    return PatientNoteResponse(
        content=note.content,
        updated_at=note.updated_at.isoformat(),
    )
