"""환자 가입 승인 요청 모델"""
import enum
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, String, Text, VARCHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class RegistrationStatus(str, enum.Enum):
    pending = "pending"      # 승인 대기
    approved = "approved"   # 승인 완료 (전화번호/비밀번호 설정 가능)
    rejected = "rejected"   # 거절
    completed = "completed" # 전화번호+비밀번호 설정 완료 → 정식 계정


class PatientRegistration(Base):
    """
    환자 가입 요청 테이블.
    - 환자가 이름/생년월일/병원/담당의를 입력하면 pending 레코드 생성
    - 의사가 승인 → approved로 변경 → 환자가 전화번호+비밀번호 설정 → completed
    - 거절 시 → rejected
    """
    __tablename__ = "patient_registrations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # 환자 입력 정보
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    birth_date: Mapped[str] = mapped_column(String(10), nullable=False)   # YYYY-MM-DD
    hospital_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("hospitals.id", ondelete="SET NULL"), nullable=True
    )
    # request_type: 'connect' (담당 연결 요청) | 'discharge' (담당 해제 요청)
    request_type: Mapped[str] = mapped_column(
        VARCHAR(20), nullable=False, default="connect", server_default="connect"
    )

    doctor_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )

    # 승인 흐름
    status: Mapped[RegistrationStatus] = mapped_column(
        Enum(RegistrationStatus, name="registration_status_enum"),
        default=RegistrationStatus.pending,
        nullable=False,
    )
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 승인 후 전화번호+비밀번호 설정 완료 시 user_id 연결
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
