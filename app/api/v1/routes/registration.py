"""회원가입 및 담당 연결 관련 API 엔드포인트"""
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import hash_password, create_access_token, get_current_user
from app.core.database import get_db
from app.crud.user import get_user_by_phone
from app.models.hospital import DoctorLicense, DoctorProfile, Hospital
from app.models.patient_assignment import PatientDoctorAssignment
from app.models.registration import PatientRegistration, RegistrationStatus
from app.models.user import User, UserRole
from app.schemas.registration import (
    DoctorRegisterStep1,
    DoctorRegisterStep2,
    PatientRegisterRequest,
    PatientRegisterComplete,
    RegistrationApprove,
    RegistrationReject,
    HospitalResponse,
    DoctorSummary,
    PatientRegistrationResponse,
    VerifyTokenResponse,
)

router = APIRouter(prefix="/registration", tags=["회원가입"])

# 임시 인메모리 토큰 저장소 (프로덕션에서는 Redis 등으로 교체)
_verify_tokens: dict[str, dict] = {}


# ── 공통: 병원 목록 ────────────────────────────────────────────

@router.get("/hospitals", response_model=list[HospitalResponse])
def list_hospitals(db: Session = Depends(get_db)):
    """병원 목록 조회"""
    return db.query(Hospital).order_by(Hospital.name).all()


# ── 의사 가입 ──────────────────────────────────────────────────

@router.post("/doctor/verify", response_model=VerifyTokenResponse)
def doctor_verify(payload: DoctorRegisterStep1, db: Session = Depends(get_db)):
    """
    의사 가입 1단계: 이름 + 생년월일 + 자격번호 + 소속병원 검증.
    시드 데이터(doctor_licenses)와 모두 일치하면 임시 토큰 반환.
    """
    lic = db.query(DoctorLicense).filter(
        DoctorLicense.license_number == payload.license_number,
        DoctorLicense.name == payload.name,
        DoctorLicense.birth_date == payload.birth_date,
        DoctorLicense.hospital_id == payload.hospital_id,
    ).first()

    if not lic:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="입력한 정보가 신장분과전문의 자격 데이터와 일치하지 않습니다.",
        )
    if lic.is_registered:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="이미 가입된 자격번호입니다.",
        )

    token = secrets.token_urlsafe(32)
    _verify_tokens[token] = {
        "name": lic.name,
        "birth_date": lic.birth_date,
        "license_number": lic.license_number,
        "hospital_id": lic.hospital_id,
        "license_id": lic.id,
    }

    return VerifyTokenResponse(
        verify_token=token,
        name=lic.name,
        hospital_id=lic.hospital_id,
        license_number=lic.license_number,
    )


@router.post("/doctor/complete", status_code=status.HTTP_201_CREATED)
def doctor_complete(payload: DoctorRegisterStep2, db: Session = Depends(get_db)):
    """의사 가입 2단계: 전화번호 + 비밀번호 설정 → 계정 생성."""
    token_data = _verify_tokens.get(payload.verify_token)
    if not token_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="유효하지 않거나 만료된 인증 토큰입니다. 1단계부터 다시 진행해주세요.",
        )

    if get_user_by_phone(db, payload.phone_number):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 사용 중인 전화번호입니다.")

    user = User(
        phone_number=payload.phone_number,
        password_hash=hash_password(payload.password),
        name=token_data["name"],
        birth_date=token_data["birth_date"],
        role=UserRole.doctor,
    )
    db.add(user)
    db.flush()

    profile = DoctorProfile(
        user_id=user.id,
        birth_date=token_data["birth_date"],
        license_number=token_data["license_number"],
        hospital_id=token_data["hospital_id"],
    )
    db.add(profile)

    lic = db.query(DoctorLicense).filter_by(id=token_data["license_id"]).first()
    if lic:
        lic.is_registered = True

    db.commit()
    _verify_tokens.pop(payload.verify_token, None)
    return {"message": "의사 계정이 생성되었습니다.", "user_id": user.id}


# ── 환자 가입 ──────────────────────────────────────────────────

@router.get("/doctors", response_model=list[DoctorSummary])
def list_doctors(hospital_id: Optional[int] = None, db: Session = Depends(get_db)):
    """병원별 의사 목록 조회 (환자 가입 시 담당의 선택용)"""
    q = db.query(User, DoctorProfile, Hospital).join(
        DoctorProfile, DoctorProfile.user_id == User.id
    ).join(
        Hospital, Hospital.id == DoctorProfile.hospital_id, isouter=True
    ).filter(User.role == UserRole.doctor, User.is_active == True)

    if hospital_id:
        q = q.filter(DoctorProfile.hospital_id == hospital_id)

    results = q.all()
    return [
        DoctorSummary(
            id=user.id,
            name=user.name,
            hospital_name=hospital.name if hospital else None,
        )
        for user, profile, hospital in results
    ]


@router.post("/patient/request", status_code=status.HTTP_201_CREATED)
def patient_request(payload: PatientRegisterRequest, db: Session = Depends(get_db)):
    """
    환자 가입 요청.
    - doctor_id가 있으면 → 담당 연결 요청(connect), 의사 승인 대기
    - doctor_id가 없으면 → 의사 없이 가입 (doctor_id=None, status=pending_no_doctor)
    """
    # 병원 확인
    if payload.hospital_id:
        hospital = db.query(Hospital).filter_by(id=payload.hospital_id).first()
        if not hospital:
            raise HTTPException(status_code=404, detail="해당 병원을 찾을 수 없습니다.")

    # 의사 확인 (선택적)
    if payload.doctor_id:
        doctor = db.query(User).filter(
            User.id == payload.doctor_id, User.role == UserRole.doctor
        ).first()
        if not doctor:
            raise HTTPException(status_code=404, detail="해당 의사를 찾을 수 없습니다.")

    reg = PatientRegistration(
        name=payload.name,
        birth_date=payload.birth_date,
        hospital_id=payload.hospital_id,
        doctor_id=payload.doctor_id,  # None 허용
        status=RegistrationStatus.pending,
        request_type="connect",
    )
    db.add(reg)
    db.commit()
    db.refresh(reg)

    if payload.doctor_id:
        msg = "인증 요청이 전송되었습니다. 담당 의사의 승인을 기다려주세요."
    else:
        msg = "가입 요청이 접수되었습니다. 담당 의사 없이 가입을 진행합니다."

    return {"message": msg, "registration_id": reg.id}


@router.delete("/patient/request/{registration_id}", status_code=status.HTTP_200_OK)
def patient_cancel_request(registration_id: int, db: Session = Depends(get_db)):
    """환자가 자신의 인증 요청을 취소"""
    reg = db.query(PatientRegistration).filter_by(id=registration_id).first()
    if not reg:
        raise HTTPException(status_code=404, detail="가입 요청을 찾을 수 없습니다.")
    if reg.status != RegistrationStatus.pending:
        raise HTTPException(status_code=400, detail="대기 중인 요청만 취소할 수 있습니다.")

    db.delete(reg)
    db.commit()
    return {"message": "가입 요청이 취소되었습니다."}


@router.get("/patient/status/{registration_id}")
def get_registration_status(registration_id: int, db: Session = Depends(get_db)):
    """가입 요청 상태 조회 (환자 폴링용)"""
    reg = db.query(PatientRegistration).filter_by(id=registration_id).first()
    if not reg:
        raise HTTPException(status_code=404, detail="가입 요청을 찾을 수 없습니다.")
    return {
        "registration_id": reg.id,
        "status": reg.status,
        "reject_reason": reg.reject_reason,
        "request_type": reg.request_type,
    }


@router.post("/patient/complete", status_code=status.HTTP_201_CREATED)
def patient_complete(payload: PatientRegisterComplete, db: Session = Depends(get_db)):
    """환자 가입 완료: 전화번호 + 비밀번호 설정 → 계정 생성."""
    reg = db.query(PatientRegistration).filter_by(id=payload.registration_id).first()
    if not reg:
        raise HTTPException(status_code=404, detail="가입 요청을 찾을 수 없습니다.")

    # 의사가 있는 경우 승인 필요 / 의사 없는 경우 pending도 허용
    if reg.doctor_id and reg.status != RegistrationStatus.approved:
        raise HTTPException(status_code=400, detail="승인된 요청에 대해서만 계정을 생성할 수 있습니다.")

    if get_user_by_phone(db, payload.phone_number):
        raise HTTPException(status_code=409, detail="이미 사용 중인 전화번호입니다.")

    user = User(
        phone_number=payload.phone_number,
        password_hash=hash_password(payload.password),
        name=reg.name,
        birth_date=reg.birth_date,
        role=UserRole.patient,
        doctor_id=reg.doctor_id,       # None 가능
        hospital_id=reg.hospital_id,
    )
    db.add(user)
    db.flush()

    reg.status = RegistrationStatus.completed
    reg.user_id = user.id

    # 담당 의사가 있으면 assignment 생성
    if reg.doctor_id:
        assignment = PatientDoctorAssignment(
            patient_id=user.id,
            doctor_id=reg.doctor_id,
            started_at=datetime.now(timezone.utc),
        )
        db.add(assignment)

    db.commit()
    return {"message": "환자 계정이 생성되었습니다.", "user_id": user.id}


# ── 의사용: 담당 연결 관리 ─────────────────────────────────────

@router.get("/doctor/pending", response_model=list[PatientRegistrationResponse])
def list_pending_registrations(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """의사 대시보드: 대기 중인 담당 연결 요청 목록 (connect + discharge)"""
    if current_user.role != UserRole.doctor:
        raise HTTPException(status_code=403, detail="의사만 접근할 수 있습니다.")

    regs = db.query(PatientRegistration).filter(
        PatientRegistration.doctor_id == current_user.id,
        PatientRegistration.status == RegistrationStatus.pending,
    ).order_by(PatientRegistration.created_at.desc()).all()

    result = []
    for reg in regs:
        hospital = db.query(Hospital).filter_by(id=reg.hospital_id).first() if reg.hospital_id else None
        result.append(PatientRegistrationResponse(
            id=reg.id,
            name=reg.name,
            birth_date=reg.birth_date,
            hospital_name=hospital.name if hospital else None,
            status=reg.status,
            created_at=reg.created_at.isoformat(),
        ))
    return result


@router.post("/doctor/approve")
def approve_registration(
    payload: RegistrationApprove,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """의사: 담당 연결 요청 승인"""
    if current_user.role != UserRole.doctor:
        raise HTTPException(status_code=403, detail="의사만 접근할 수 있습니다.")

    reg = db.query(PatientRegistration).filter_by(
        id=payload.registration_id, doctor_id=current_user.id
    ).first()
    if not reg:
        raise HTTPException(status_code=404, detail="요청을 찾을 수 없습니다.")
    if reg.status != RegistrationStatus.pending:
        raise HTTPException(status_code=400, detail="대기 중인 요청만 승인할 수 있습니다.")

    reg.status = RegistrationStatus.approved

    # discharge 요청인 경우 → 즉시 담당 해제 처리
    if reg.request_type == "discharge" and reg.user_id:
        _handle_discharge_approval(db, current_user.id, reg.user_id)

    db.commit()
    action = "담당 해제가" if reg.request_type == "discharge" else "담당 연결 요청이"
    return {"message": f"{action} 승인되었습니다."}


def _handle_discharge_approval(db: Session, doctor_id: int, patient_id: int):
    """담당 해제 승인 처리 — assignment ended_at 설정"""
    assignment = (
        db.query(PatientDoctorAssignment)
        .filter(
            PatientDoctorAssignment.doctor_id == doctor_id,
            PatientDoctorAssignment.patient_id == patient_id,
            PatientDoctorAssignment.ended_at.is_(None),
        )
        .first()
    )
    if assignment:
        assignment.ended_at = datetime.now(timezone.utc)

    # users.doctor_id NULL 처리
    patient = db.query(User).filter_by(id=patient_id).first()
    if patient and patient.doctor_id == doctor_id:
        patient.doctor_id = None


@router.post("/doctor/reject")
def reject_registration(
    payload: RegistrationReject,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """의사: 담당 연결/해제 요청 거절"""
    if current_user.role != UserRole.doctor:
        raise HTTPException(status_code=403, detail="의사만 접근할 수 있습니다.")

    reg = db.query(PatientRegistration).filter_by(
        id=payload.registration_id, doctor_id=current_user.id
    ).first()
    if not reg:
        raise HTTPException(status_code=404, detail="요청을 찾을 수 없습니다.")
    if reg.status != RegistrationStatus.pending:
        raise HTTPException(status_code=400, detail="대기 중인 요청만 거절할 수 있습니다.")

    reg.status = RegistrationStatus.rejected
    reg.reject_reason = payload.reason
    db.commit()
    return {"message": "요청이 거절되었습니다."}


# ── 환자: 담당 해제 요청 ────────────────────────────────────────

class DischargeRequestPayload(BaseModel):
    reason: Optional[str] = None

from pydantic import BaseModel as PydanticBaseModel
class DischargeRequestPayload(PydanticBaseModel):
    reason: Optional[str] = None


@router.post("/patient/discharge-request", status_code=status.HTTP_201_CREATED)
def patient_discharge_request(
    payload: DischargeRequestPayload,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """환자: 담당 해제 요청 전송"""
    if current_user.role != UserRole.patient:
        raise HTTPException(status_code=403, detail="환자만 접근할 수 있습니다.")
    if not current_user.doctor_id:
        raise HTTPException(status_code=400, detail="현재 담당 의사가 없습니다.")

    # 이미 pending인 해제 요청이 있는지 확인
    existing = db.query(PatientRegistration).filter(
        PatientRegistration.user_id == current_user.id,
        PatientRegistration.doctor_id == current_user.doctor_id,
        PatientRegistration.request_type == "discharge",
        PatientRegistration.status == RegistrationStatus.pending,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="이미 대기 중인 담당 해제 요청이 있습니다.")

    reg = PatientRegistration(
        name=current_user.name,
        birth_date=current_user.birth_date,
        hospital_id=current_user.hospital_id,
        doctor_id=current_user.doctor_id,
        status=RegistrationStatus.pending,
        request_type="discharge",
        user_id=current_user.id,
        reject_reason=payload.reason,
    )
    db.add(reg)
    db.commit()
    db.refresh(reg)
    return {"message": "담당 해제 요청이 전송되었습니다.", "registration_id": reg.id}
