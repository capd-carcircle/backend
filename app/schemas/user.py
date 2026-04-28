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
    token_type: str = "bearer"
    user_id: int
    name: str
    role: UserRole


# ── 유저 정보 응답 ─────────────────────────────────────────────
class UserResponse(BaseModel):
    id: int
    phone_number: str
    name: str
    birth_date: Optional[str] = None
    role: UserRole
    is_active: bool
    doctor_id: Optional[int] = None   # 환자 전용: 담당 의사 ID

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
    hospital_name: Optional[str] = None
    doctor_name: Optional[str] = None
    self_memo: Optional[str] = None
    role: UserRole

    model_config = {"from_attributes": True}
