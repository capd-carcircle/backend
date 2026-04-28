from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth import (
    verify_password,
    hash_password,
    create_access_token,
    get_current_user,
)
from app.core.database import get_db
from app.crud.user import get_user_by_phone
from app.models.hospital import DoctorProfile, Hospital
from app.models.user import User, UserRole
from app.schemas.user import (
    LoginRequest, TokenResponse, UserResponse,
    UpdateMeRequest, DoctorProfileResponse, PatientProfileResponse,
)

router = APIRouter(prefix="/auth", tags=["인증"])


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    """전화번호 + 비밀번호로 로그인 → JWT 반환"""
    user = get_user_by_phone(db, phone_number=payload.phone_number)

    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="전화번호 또는 비밀번호가 올바르지 않습니다.",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="비활성화된 계정입니다.",
        )

    token = create_access_token({"sub": str(user.id), "role": user.role})
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        name=user.name,
        role=user.role,
    )


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    """현재 로그인한 유저 기본 정보 반환"""
    return current_user


@router.get("/me/profile")
def get_my_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """역할별 상세 프로필 반환 (마이페이지용)"""
    if current_user.role == UserRole.doctor:
        profile = db.query(DoctorProfile).filter_by(user_id=current_user.id).first()
        hospital = db.query(Hospital).filter_by(id=profile.hospital_id).first() if profile else None
        return DoctorProfileResponse(
            id=current_user.id,
            name=current_user.name,
            phone_number=current_user.phone_number,
            birth_date=current_user.birth_date,
            license_number=profile.license_number if profile else None,
            hospital_name=hospital.name if hospital else None,
            role=current_user.role,
        )
    else:
        hospital = db.query(Hospital).filter_by(id=current_user.hospital_id).first() if current_user.hospital_id else None
        doctor = db.query(User).filter_by(id=current_user.doctor_id).first() if current_user.doctor_id else None
        return PatientProfileResponse(
            id=current_user.id,
            name=current_user.name,
            phone_number=current_user.phone_number,
            birth_date=current_user.birth_date,
            hospital_name=hospital.name if hospital else None,
            doctor_name=doctor.name if doctor else None,
            self_memo=current_user.self_memo,
            role=current_user.role,
        )


@router.patch("/me")
def update_me(
    payload: UpdateMeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """마이페이지 수정 — 전화번호 / 비밀번호 / 환자 자기메모"""
    # 전화번호 변경
    if payload.phone_number and payload.phone_number != current_user.phone_number:
        existing = get_user_by_phone(db, payload.phone_number)
        if existing:
            raise HTTPException(status_code=409, detail="이미 사용 중인 전화번호입니다.")
        current_user.phone_number = payload.phone_number

    # 비밀번호 변경
    if payload.new_password:
        if not payload.current_password:
            raise HTTPException(status_code=400, detail="현재 비밀번호를 입력해주세요.")
        if not verify_password(payload.current_password, current_user.password_hash):
            raise HTTPException(status_code=400, detail="현재 비밀번호가 올바르지 않습니다.")
        if len(payload.new_password) < 6:
            raise HTTPException(status_code=400, detail="비밀번호는 6자 이상이어야 합니다.")
        current_user.password_hash = hash_password(payload.new_password)

    # 환자 자기 메모
    if payload.self_memo is not None:
        if current_user.role != UserRole.patient:
            raise HTTPException(status_code=403, detail="환자만 자기 메모를 작성할 수 있습니다.")
        current_user.self_memo = payload.self_memo

    db.commit()
    return {"message": "프로필이 업데이트되었습니다."}
