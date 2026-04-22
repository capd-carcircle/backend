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
    role: UserRole
    is_active: bool

    model_config = {"from_attributes": True}
