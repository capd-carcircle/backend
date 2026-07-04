from typing import Optional
from pydantic import BaseModel
from app.models.user import UserRole


# ── 로그인 요청 ────────────────────────────────────────────────
class LoginRequest(BaseModel):
    phone_number: str
    password: str


# ── 토큰 응답 ──────────────────────────────────────────────────
class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: int
    name: str
    role: UserRole
    doctor_id: Optional[int] = None   # 환자 전용: 담당 의사 ID (없으면 null)


# ── 유저 정보 응답 ─────────────────────────────────────────────
class UserResponse(BaseModel):
    id: int
    phone_number: str
    name: str
    birth_date: Optional[str] = None
    role: UserRole
    is_active: bool
    doctor_id: Optional[int] = None   # 환자 전용: 담당 의사 ID
    gender: Optional[str] = None      # 환자 전용: 'm' | 'f'
    address: Optional[str] = None     # 환자 전용: 거주지

    model_config = {"from_attributes": True}


# ── 마이페이지 수정 요청 ───────────────────────────────────────
class UpdateMeRequest(BaseModel):
    # 프로필 수정 (이름/생년월일/전화번호 변경 시 current_password 필수)
    name: Optional[str] = None
    birth_date: Optional[str] = None
    phone_number: Optional[str] = None
    current_password: Optional[str] = None   # 프로필·비밀번호 변경 시 필수
    new_password: Optional[str] = None       # 비밀번호 변경 시 필수
    self_memo: Optional[str] = None          # 환자 전용
    address: Optional[str] = None           # 환자 전용: 거주지 (변경 가능)


# ── 의사 프로필 응답 (마이페이지용) ────────────────────────────
class DoctorProfileResponse(BaseModel):
    id: int
    name: str
    phone_number: str
    birth_date: Optional[str] = None
    license_number: Optional[str] = None
    hospital_name: Optional[str] = None
    role: UserRole

    model_config = {"from_attributes": True}


# ── 환자 프로필 응답 (마이페이지 + 의사용 상세) ────────────────
class PatientProfileResponse(BaseModel):
    id: int
    name: str
    phone_number: str
    birth_date: Optional[str] = None
    hospital_name: Optional[str] = None   # 담당 의사의 소속 병원
    doctor_name: Optional[str] = None
    doctor_id: Optional[int] = None
    doctor_phone: Optional[str] = None    # 담당 의사 전화번호
    doctor_hospital: Optional[str] = None # 담당 의사 소속 병원명 (명시적 분리)
    self_memo: Optional[str] = None
    gender: Optional[str] = None    # 'm' | 'f'
    address: Optional[str] = None
    role: UserRole

    model_config = {"from_attributes": True}
