"""의사-환자 담당 관계 히스토리 모델"""
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PatientDoctorAssignment(Base):
    """
    의사-환자 담당 기간 테이블.

    - ended_at IS NULL  → 현재 담당 의사
    - ended_at NOT NULL → 과거 담당 (종료됨)
    - 한 환자에 대해 ended_at IS NULL인 레코드는 최대 1개 (부분 유니크 인덱스 보장)
    """
    __tablename__ = "patient_doctor_assignments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    patient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doctor_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
