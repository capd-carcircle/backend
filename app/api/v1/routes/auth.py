from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth import (
    verify_password,
    hash_password,
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
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
from pydantic import BaseModel

class RefreshRequest(BaseModel):
    refresh_token: str

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

    token_data = {"sub": str(user.id), "role": user.role}
    access_token = create_access_token(token_data)
    refresh_token = create_refresh_token(token_data)

    # refresh token DB 저장
    user.refresh_token = refresh_token
    db.commit()

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user_id=user.id,
        name=user.name,
        role=user.role,
    )


@router.post("/refresh")
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
    """refresh_token → 새 access_token + 새 refresh_token 발급 (토큰 회전)"""
    from app.crud.user import get_user_by_id
    token_payload = decode_refresh_token(payload.refresh_token)
    user_id = token_payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="유효하지 않은 리프레시 토큰입니다.")
    user = get_user_by_id(db, user_id=int(user_id))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="사용자를 찾을 수 없습니다.")

    # DB에 저장된 refresh token과 비교 (탈취 토큰 차단)
    if user.refresh_token != payload.refresh_token:
        raise HTTPException(status_code=401, detail="유효하지 않은 리프레시 토큰입니다.")

    # 토큰 회전: 새 access + 새 refresh 발급 후 DB 갱신
    token_data = {"sub": str(user.id), "role": user.role}
    new_access_token = create_access_token(token_data)
    new_refresh_token = create_refresh_token(token_data)
    user.refresh_token = new_refresh_token
    db.commit()

    return {"access_token": new_access_token, "refresh_token": new_refresh_token, "token_type": "bearer"}


@router.post("/logout")
def logout(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """로그아웃 — DB의 refresh token 무효화"""
    current_user.refresh_token = None
    db.commit()
    return {"message": "로그아웃되었습니다."}


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
        doctor = db.query(User).filter_by(id=current_user.doctor_id).first() if current_user.doctor_id else None
        # 담당 의사의 소속 병원 조회
        doctor_hospital_name: str | None = None
        doctor_phone: str | None = None
        if doctor:
            doctor_phone = doctor.phone_number
            doc_profile = db.query(DoctorProfile).filter_by(user_id=doctor.id).first()
            if doc_profile and doc_profile.hospital_id:
                doc_hosp = db.query(Hospital).filter_by(id=doc_profile.hospital_id).first()
                doctor_hospital_name = doc_hosp.name if doc_hosp else None
        return PatientProfileResponse(
            id=current_user.id,
            name=current_user.name,
            phone_number=current_user.phone_number,
            birth_date=current_user.birth_date,
            hospital_name=doctor_hospital_name,  # 담당 의사의 병원으로 표시
            doctor_name=doctor.name if doctor else None,
            doctor_id=current_user.doctor_id,
            doctor_phone=doctor_phone,
            doctor_hospital=doctor_hospital_name,
            self_memo=current_user.self_memo,
            gender=current_user.gender,
            address=current_user.address,
            role=current_user.role,
        )


@router.patch("/me")
def update_me(
    payload: UpdateMeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """마이페이지 수정 — 비밀번호/환자 자기메모

    비밀번호 변경 시 current_password 필수.
    self_memo(환자 전용)은 별도 비밀번호 확인 없이 저장 가능.
    이름·생년월일·전화번호는 UI에서 수정 불가 처리됨.
    """
    # 프로필 변경(이름/전화번호/생년월일) 또는 비밀번호 변경 시 현재 비밀번호 필수
    profile_changing = payload.name or payload.phone_number or payload.birth_date or payload.new_password
    if profile_changing:
        if not payload.current_password:
            raise HTTPException(status_code=400, detail="현재 비밀번호를 입력해주세요.")
        if not verify_password(payload.current_password, current_user.password_hash):
            raise HTTPException(status_code=400, detail="현재 비밀번호가 올바르지 않습니다.")

    # 이름/전화번호/생년월일 수정
    if payload.name:
        current_user.name = payload.name
    if payload.phone_number:
        current_user.phone_number = payload.phone_number
    if payload.birth_date is not None and current_user.role != UserRole.patient:
        # 의사 생년월일은 doctor_profiles 테이블에 있으나 일단 skip (별도 처리 필요 시 추가)
        pass

    # 비밀번호 변경
    if payload.new_password:
        if len(payload.new_password) < 6:
            raise HTTPException(status_code=400, detail="비밀번호는 6자 이상이어야 합니다.")
        current_user.password_hash = hash_password(payload.new_password)

    # 환자 자기 메모
    if payload.self_memo is not None:
        if current_user.role != UserRole.patient:
            raise HTTPException(status_code=403, detail="환자만 자기 메모를 작성할 수 있습니다.")
        current_user.self_memo = payload.self_memo

    # 환자 거주지
    if payload.address is not None:
        if current_user.role != UserRole.patient:
            raise HTTPException(status_code=403, detail="환자만 거주지를 수정할 수 있습니다.")
        current_user.address = payload.address

    db.commit()
    return {"message": "프로필이 업데이트되었습니다.", "name": current_user.name}
