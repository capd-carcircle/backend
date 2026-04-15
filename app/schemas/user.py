from pydantic import BaseModel, EmailStr
from app.models.user import UserRole


# ── 로그인 요청 ────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: EmailStr
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
    email: str
    name: str
    role: UserRole
    is_active: bool

    model_config = {"from_attributes": True}
