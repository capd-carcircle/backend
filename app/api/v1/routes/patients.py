"""
patients.py — 의사용 환자 관리 API

담당 관계는 patient_doctor_assignments 테이블 기준.
- scope=current  : ended_at IS NULL 인 현재 담당 환자
- scope=past     : ended_at IS NOT NULL 인 과거 담당 환자
- 기록 접근 범위 : 현재 담당 → 전체 기록 / 과거 담당 → 담당 기간(~ended_at) 내 기록
"""
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, or_
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.hospital import Hospital
from app.models.patient_assignment import PatientDoctorAssignment
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
    birth_date:   Optional[str] = None
    gender:       Optional[str] = None

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
    id:                     int
    name:                   str
    phone_number:           str
    birth_date:             Optional[str] = None
    gender:                 Optional[str] = None   # 'm' | 'f'
    total_records:          int
    last_record_date:       Optional[str]
    last_submitted_at:      Optional[str]
    latest_risk_level:      Optional[str]
    days_since_last_record: Optional[int]
    is_current:             bool       # True = 현재 담당
    assignment_started_at:  Optional[str]
    assignment_ended_at:    Optional[str]


# ── 헬퍼 ──────────────────────────────────────────────────

def _require_doctor(current_user: User) -> None:
    if current_user.role != UserRole.doctor:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="의사만 접근할 수 있습니다.",
        )


def _get_assignment(
    db: Session,
    doctor_id: int,
    patient_id: int,
) -> Optional[PatientDoctorAssignment]:
    """해당 의사-환자 assignment 중 가장 최근 것 반환 (현재·과거 모두)"""
    return (
        db.query(PatientDoctorAssignment)
        .filter(
            PatientDoctorAssignment.doctor_id == doctor_id,
            PatientDoctorAssignment.patient_id == patient_id,
        )
        .order_by(PatientDoctorAssignment.started_at.desc())
        .first()
    )


def _get_current_assignment(
    db: Session,
    doctor_id: int,
    patient_id: int,
) -> Optional[PatientDoctorAssignment]:
    """현재 담당 assignment (ended_at IS NULL, started_at <= now)"""
    now = datetime.now(timezone.utc)
    return (
        db.query(PatientDoctorAssignment)
        .filter(
            PatientDoctorAssignment.doctor_id == doctor_id,
            PatientDoctorAssignment.patient_id == patient_id,
            PatientDoctorAssignment.ended_at.is_(None),
            PatientDoctorAssignment.started_at <= now,
        )
        .first()
    )


# ── 환자 목록 ──────────────────────────────────────────────

@router.get(
    "",
    response_model=List[PatientInfo],
    summary="나의 환자 목록 (현재 담당)",
)
def list_patients(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[PatientInfo]:
    _require_doctor(current_user)

    # assignment 기반 현재 담당 + 레거시(users.doctor_id) 호환
    assign_patient_ids = (
        db.query(PatientDoctorAssignment.patient_id)
        .filter(
            PatientDoctorAssignment.doctor_id == current_user.id,
            PatientDoctorAssignment.ended_at.is_(None),
            PatientDoctorAssignment.started_at <= datetime.now(timezone.utc),
        )
        .subquery()
    )
    patients = (
        db.query(User)
        .filter(
            User.role == UserRole.patient,
            User.is_active == True,
            or_(
                User.id.in_(assign_patient_ids),
                User.doctor_id == current_user.id,
            ),
        )
        .order_by(User.name)
        .all()
    )
    return [PatientInfo(id=p.id, name=p.name, phone_number=p.phone_number, birth_date=p.birth_date, gender=p.gender) for p in patients]


@router.get(
    "/overview",
    response_model=List[PatientOverview],
    summary="환자별 요약 정보",
    description="scope=current(기본) 현재 담당, scope=past 과거 담당",
)
def list_patients_overview(
    scope: str = Query(default="current", description="current | past"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[PatientOverview]:
    _require_doctor(current_user)

    is_current = (scope != "past")

    # assignment 레코드 조회
    q = db.query(PatientDoctorAssignment).filter(
        PatientDoctorAssignment.doctor_id == current_user.id,
    )
    if is_current:
        # ended_at IS NULL OR users.doctor_id == current_user.id (레거시)
        q = q.filter(PatientDoctorAssignment.ended_at.is_(None))
    else:
        # 현재 이 의사에게 다시 담당 중인 환자는 과거 목록에서 제외
        current_patient_ids_sq = (
            db.query(PatientDoctorAssignment.patient_id)
            .filter(
                PatientDoctorAssignment.doctor_id == current_user.id,
                PatientDoctorAssignment.ended_at.is_(None),
            )
            .subquery()
        )
        q = q.filter(
            PatientDoctorAssignment.ended_at.isnot(None),
            PatientDoctorAssignment.patient_id.notin_(current_patient_ids_sq),
        )

    assignments = q.order_by(PatientDoctorAssignment.started_at.desc()).all()

    # 레거시 호환: assignment가 없고 users.doctor_id로 연결된 현재 환자
    legacy_patients: List[User] = []
    if is_current:
        assigned_patient_ids = {a.patient_id for a in assignments}
        legacy_patients = (
            db.query(User)
            .filter(
                User.role == UserRole.patient,
                User.is_active == True,
                User.doctor_id == current_user.id,
                User.id.notin_(assigned_patient_ids) if assigned_patient_ids else True,
            )
            .all()
        )

    today = datetime.now(timezone.utc).date()
    result: List[PatientOverview] = []

    def _make_overview(patient: User, assignment: Optional[PatientDoctorAssignment], curr: bool) -> PatientOverview:
        # 접근 가능한 기록 범위 결정
        records_q = db.query(DailyRecord).filter(DailyRecord.patient_id == patient.id)
        if not curr and assignment and assignment.ended_at:
            # 과거 담당: 담당 종료일까지의 기록만
            records_q = records_q.filter(
                DailyRecord.record_date <= assignment.ended_at.date()
            )
        records = records_q.order_by(desc(DailyRecord.record_date)).all()

        submitted = [r for r in records if r.status.value in ("submitted", "reviewed")]
        last_rec  = records[0] if records else None
        last_sub  = submitted[0] if submitted else None

        days_since = (today - last_rec.record_date).days if last_rec else None

        return PatientOverview(
            id                    = patient.id,
            name                  = patient.name,
            phone_number          = patient.phone_number,
            birth_date            = patient.birth_date,
            gender                = patient.gender,
            total_records         = len(records),
            last_record_date      = last_rec.record_date.isoformat() if last_rec else None,
            last_submitted_at     = last_sub.submitted_at.isoformat() if last_sub and last_sub.submitted_at else None,
            latest_risk_level     = last_sub.risk_level.value if last_sub and last_sub.risk_level else None,
            days_since_last_record= days_since,
            is_current            = curr,
            assignment_started_at = assignment.started_at.isoformat() if assignment else None,
            assignment_ended_at   = assignment.ended_at.isoformat() if assignment and assignment.ended_at else None,
        )

    # assignment 기반 환자 처리 — 배치 로딩으로 N+1 방지
    asgn_patient_ids = list({a.patient_id for a in assignments})
    patients_map: dict[int, User] = {
        p.id: p
        for p in db.query(User).filter(User.id.in_(asgn_patient_ids)).all()
    }
    seen_patients: set[int] = set()
    for asgn in assignments:
        if asgn.patient_id in seen_patients:
            continue
        seen_patients.add(asgn.patient_id)
        patient = patients_map.get(asgn.patient_id)
        if not patient:
            continue
        result.append(_make_overview(patient, asgn, is_current))

    # 레거시 환자 추가
    for patient in legacy_patients:
        result.append(_make_overview(patient, None, True))

    result.sort(key=lambda r: r.name)
    return result


# ── 환자 기록 목록 (의사용) ─────────────────────────────────

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

    # 접근 권한 확인 — 현재 또는 과거 담당이면 허용
    assignment = _get_assignment(db, current_user.id, patient_id)
    # 레거시 호환
    patient = db.query(User).filter(User.id == patient_id, User.role == UserRole.patient).first()
    if not patient:
        raise HTTPException(status_code=404, detail="환자를 찾을 수 없습니다.")

    has_access = (
        assignment is not None
        or patient.doctor_id == current_user.id
    )
    if not has_access:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

    # 기록 범위 제한 (과거 담당이면 담당 기간 내 기록만)
    records_q = db.query(DailyRecord).filter(DailyRecord.patient_id == patient_id)
    is_current = assignment is None or assignment.ended_at is None
    if not is_current and assignment and assignment.ended_at:
        records_q = records_q.filter(
            DailyRecord.record_date <= assignment.ended_at.date()
        )
    records = records_q.order_by(desc(DailyRecord.record_date)).all()

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
    self_memo:    Optional[str]
    joined_at:    Optional[str]
    is_current_patient: bool    # 현재 담당 여부
    gender:       Optional[str] = None
    address:      Optional[str] = None


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

    patient = db.query(User).filter(User.id == patient_id, User.role == UserRole.patient).first()
    if not patient:
        raise HTTPException(status_code=404, detail="환자를 찾을 수 없습니다.")

    assignment = _get_assignment(db, current_user.id, patient_id)
    has_access = assignment is not None or patient.doctor_id == current_user.id
    if not has_access:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

    is_current = (
        (assignment is not None and assignment.ended_at is None)
        or patient.doctor_id == current_user.id
    )

    hospital = db.query(Hospital).filter_by(id=patient.hospital_id).first() if patient.hospital_id else None
    doctor   = db.query(User).filter_by(id=patient.doctor_id).first() if patient.doctor_id else None

    return PatientDetailProfile(
        id                  = patient.id,
        name                = patient.name,
        phone_number        = patient.phone_number,
        birth_date          = patient.birth_date,
        hospital_name       = hospital.name if hospital else None,
        doctor_name         = doctor.name if doctor else None,
        self_memo           = patient.self_memo,
        joined_at           = patient.created_at.isoformat() if patient.created_at else None,
        is_current_patient  = is_current,
        gender              = patient.gender,
        address             = patient.address,
    )


# ── 담당 해제 ──────────────────────────────────────────────

@router.post(
    "/{patient_id}/discharge",
    summary="담당 환자 해제 (의사용)",
)
def discharge_patient(
    patient_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_doctor(current_user)

    patient = db.query(User).filter(User.id == patient_id, User.role == UserRole.patient).first()
    if not patient:
        raise HTTPException(status_code=404, detail="환자를 찾을 수 없습니다.")

    assignment = _get_current_assignment(db, current_user.id, patient_id)

    # 레거시: assignment가 없고 users.doctor_id로 연결된 경우 → assignment 신규 생성 후 종료
    if not assignment and patient.doctor_id == current_user.id:
        assignment = PatientDoctorAssignment(
            patient_id=patient_id,
            doctor_id=current_user.id,
            started_at=patient.created_at or datetime.now(timezone.utc),
        )
        db.add(assignment)
        db.flush()

    if not assignment:
        raise HTTPException(status_code=404, detail="현재 담당 관계가 없습니다.")

    now = datetime.now(timezone.utc)
    assignment.ended_at = now

    # users.doctor_id도 NULL로
    if patient.doctor_id == current_user.id:
        patient.doctor_id = None

    db.commit()
    return {"message": f"{patient.name} 환자의 담당이 해제되었습니다."}


# ── 담당 연결 (의사가 직접 연결) ──────────────────────────────

class AssignPatientRequest(BaseModel):
    patient_id: int


@router.post(
    "/assign",
    summary="환자 담당 연결 (의사가 직접)",
    status_code=status.HTTP_201_CREATED,
)
def assign_patient(
    payload: AssignPatientRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_doctor(current_user)

    patient = db.query(User).filter(User.id == payload.patient_id, User.role == UserRole.patient).first()
    if not patient:
        raise HTTPException(status_code=404, detail="환자를 찾을 수 없습니다.")

    # 이미 현재 담당 의사가 있는지 확인
    existing = (
        db.query(PatientDoctorAssignment)
        .filter(
            PatientDoctorAssignment.patient_id == payload.patient_id,
            PatientDoctorAssignment.ended_at.is_(None),
        )
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="이미 담당 의사가 있습니다. 먼저 기존 담당을 해제하세요.")

    assignment = PatientDoctorAssignment(
        patient_id=payload.patient_id,
        doctor_id=current_user.id,
        started_at=datetime.now(timezone.utc),
    )
    db.add(assignment)

    # users.doctor_id 동기화
    patient.doctor_id = current_user.id

    db.commit()
    return {"message": f"{patient.name} 환자의 담당 연결이 완료되었습니다."}


# ── 의사 메모 ──────────────────────────────────────────────

class PatientNoteResponse(BaseModel):
    content:    Optional[str]
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
    note = db.query(PatientNote).filter_by(doctor_id=current_user.id, patient_id=patient_id).first()
    if not note:
        return PatientNoteResponse(content=None, updated_at=None)
    return PatientNoteResponse(content=note.content, updated_at=note.updated_at.isoformat())


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
    # 현재 담당 의사만 메모 작성 가능
    if not _get_current_assignment(db, current_user.id, patient_id):
        raise HTTPException(status_code=403, detail="현재 담당 의사만 메모를 작성할 수 있습니다.")

    note = db.query(PatientNote).filter_by(doctor_id=current_user.id, patient_id=patient_id).first()
    if note:
        note.content = payload.content
    else:
        note = PatientNote(doctor_id=current_user.id, patient_id=patient_id, content=payload.content)
        db.add(note)

    db.commit()
    db.refresh(note)
    return PatientNoteResponse(content=note.content, updated_at=note.updated_at.isoformat())


# ── 환자 수치 트렌드 (의사용) ──────────────────────────────────

@router.get(
    "/{patient_id}/trend",
    summary="환자 수치 최근 추이 (의사용)",
)
def get_patient_trend(
    patient_id: int,
    days: int = Query(default=14, ge=3, le=90),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    _require_doctor(current_user)

    patient = db.query(User).filter(User.id == patient_id, User.role == UserRole.patient).first()
    if not patient:
        raise HTTPException(status_code=404, detail="환자를 찾을 수 없습니다.")

    assignment = _get_assignment(db, current_user.id, patient_id)
    if not assignment and patient.doctor_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    records = (
        db.query(DailyRecord)
        .filter(
            DailyRecord.patient_id == patient_id,
            DailyRecord.record_date >= cutoff,
            DailyRecord.status.in_(["submitted", "reviewed"]),
        )
        .order_by(DailyRecord.record_date)
        .all()
    )

    return [
        {
            "record_date":            r.record_date.isoformat(),
            "weight":                 float(r.weight) if r.weight is not None else None,
            "total_ultrafiltration":  float(r.total_ultrafiltration) if r.total_ultrafiltration is not None else None,
            "blood_pressure":         r.blood_pressure,
            "risk_level":             r.risk_level.value if r.risk_level else None,
        }
        for r in records
    ]


# ── 기록 PDF 내보내기 (의사용) ─────────────────────────────────

@router.get(
    "/{patient_id}/records-export",
    summary="기록 내보내기 HTML (의사용)",
)
def export_patient_records(
    patient_id: int,
    start_date: Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    end_date:   Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    _require_doctor(current_user)

    patient = db.query(User).filter(User.id == patient_id, User.role == UserRole.patient).first()
    if not patient:
        raise HTTPException(status_code=404, detail="환자를 찾을 수 없습니다.")

    assignment = _get_assignment(db, current_user.id, patient_id)
    if not assignment and patient.doctor_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

    hospital = db.query(Hospital).filter_by(id=patient.hospital_id).first() if patient.hospital_id else None

    records_q = db.query(DailyRecord).filter(
        DailyRecord.patient_id == patient_id,
        DailyRecord.status.in_(["submitted", "reviewed"]),
    )
    if start_date:
        records_q = records_q.filter(DailyRecord.record_date >= date.fromisoformat(start_date))
    if end_date:
        records_q = records_q.filter(DailyRecord.record_date <= date.fromisoformat(end_date))
    records = records_q.order_by(DailyRecord.record_date).all()

    risk_label = {"urgent": "긴급", "caution": "주의", "normal": "정상"}

    return {
        "patient": {
            "name":         patient.name,
            "birth_date":   patient.birth_date,
            "gender":       "남" if patient.gender == "m" else "여" if patient.gender == "f" else None,
            "phone_number": patient.phone_number,
            "hospital":     hospital.name if hospital else None,
        },
        "period": {"start": start_date, "end": end_date},
        "records": [
            {
                "record_date":            r.record_date.isoformat(),
                "weight":                 float(r.weight) if r.weight is not None else None,
                "blood_pressure":         r.blood_pressure,
                "total_ultrafiltration":  float(r.total_ultrafiltration) if r.total_ultrafiltration is not None else None,
                "fasting_blood_glucose":  float(r.fasting_blood_glucose) if r.fasting_blood_glucose is not None else None,
                "turbid_peritoneal":      r.turbid_peritoneal,
                "risk_level":             risk_label.get(r.risk_level.value, "") if r.risk_level else "",
                "memo":                   r.memo or "",
            }
            for r in records
        ],
        "doctor_name": current_user.name,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 담당의사 인수인계 (의사용) ─────────────────────────────────

class HandoverRequest(BaseModel):
    new_doctor_id: int


@router.post(
    "/{patient_id}/handover",
    summary="담당 환자 인수인계 (다른 의사에게 자동 이관)",
    status_code=status.HTTP_200_OK,
)
def handover_patient(
    patient_id: int,
    payload: HandoverRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """현재 담당 의사가 다른 의사에게 환자를 인수인계.
    1) 현재 담당 assignment 종료
    2) 새 의사에게 즉시(자동 승인) 연결
    """
    _require_doctor(current_user)

    # 새 의사 존재 확인
    new_doctor = db.query(User).filter(
        User.id == payload.new_doctor_id, User.role == UserRole.doctor
    ).first()
    if not new_doctor:
        raise HTTPException(status_code=404, detail="대상 의사를 찾을 수 없습니다.")
    if new_doctor.id == current_user.id:
        raise HTTPException(status_code=400, detail="자기 자신에게는 인수인계할 수 없습니다.")

    patient = db.query(User).filter(User.id == patient_id, User.role == UserRole.patient).first()
    if not patient:
        raise HTTPException(status_code=404, detail="환자를 찾을 수 없습니다.")

    # 현재 담당 관계 확인
    assignment = _get_current_assignment(db, current_user.id, patient_id)
    if not assignment and patient.doctor_id != current_user.id:
        raise HTTPException(status_code=403, detail="현재 담당 환자가 아닙니다.")

    now = datetime.now(timezone.utc)

    # 1) 이 환자에 대한 모든 active assignment 종료 (unique 제약 위반 방지)
    active_assignments = (
        db.query(PatientDoctorAssignment)
        .filter(
            PatientDoctorAssignment.patient_id == patient_id,
            PatientDoctorAssignment.ended_at.is_(None),
        )
        .all()
    )
    for a in active_assignments:
        a.ended_at = now

    # flush: UPDATE 먼저 적용 후 INSERT (unique constraint 순서 보장)
    db.flush()

    # 2) 새 의사 assignment 생성 (자동 승인)
    new_assignment = PatientDoctorAssignment(
        patient_id=patient_id,
        doctor_id=new_doctor.id,
        started_at=now,
    )
    db.add(new_assignment)

    # 3) users.doctor_id 업데이트
    patient.doctor_id = new_doctor.id

    db.commit()
    return {
        "message": f"{patient.name} 환자가 {new_doctor.name} 의사에게 인수인계되었습니다.",
        "new_doctor_name": new_doctor.name,
    }
