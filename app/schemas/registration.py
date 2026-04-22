"""회원가입 관련 스키마"""
from pydantic import BaseModel
from typing import Optional
from app.models.registration import RegistrationStatus


# ── 의사 가입 ───────────────────────────────────────────────────

class DoctorRegisterStep1(BaseModel):
    """1단계: 자격 검증 요청"""
    name: str
    birth_date: str          # YYYY-MM-DD
    license_number: str
    hospital_id: int


class DoctorRegisterStep2(BaseModel):
    """2단계: 전화번호 + 비밀번호 설정"""
    phone_number: str
    password: str
    # 1단계 검증 토큰 (서버에서 발급한 임시 토큰)
    verify_token: str


# ── 환자 가입 ───────────────────────────────────────────────────

class PatientRegisterRequest(BaseModel):
    """환자 인증 요청 (승인 대기)"""
    name: str
    birth_date: str          # YYYY-MM-DD
    hospital_id: int
    doctor_id: int


class PatientRegisterComplete(BaseModel):
    """승인 후 전화번호 + 비밀번호 설정"""
    registration_id: int
    phone_number: str
    password: str


# ── 의사 대시보드: 승인/거절 ────────────────────────────────────

class RegistrationApprove(BaseModel):
    registration_id: int


class RegistrationReject(BaseModel):
    registration_id: int
    reason: Optional[str] = None


# ── 응답 스키마 ─────────────────────────────────────────────────

class HospitalResponse(BaseModel):
    id: int
    name: str
    address: Optional[str] = None

    model_config = {"from_attributes": True}


class DoctorSummary(BaseModel):
    id: int
    name: str
    hospital_name: Optional[str] = None


class PatientRegistrationResponse(BaseModel):
    id: int
    name: str
    birth_date: str
    hospital_name: Optional[str] = None
    status: RegistrationStatus
    created_at: str

    model_config = {"from_attributes": True}


class VerifyTokenResponse(BaseModel):
    """의사 1단계 검증 성공 시 반환하는 임시 토큰"""
    verify_token: str
    name: str
    hospital_id: int
    license_number: str
