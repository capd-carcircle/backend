"""의사가 특정 환자에 대해 작성하는 단일 메모"""
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PatientNote(Base):
    """
    의사 전용 환자 메모 — 환자는 열람 불가.
    (doctor_id, patient_id) UNIQUE → 의사당 환자 1개 메모 유지.
    AI 질문 생성 시 historical_context에 포함 예정.
    """
    __tablename__ = "patient_notes"
    __table_args__ = (
        UniqueConstraint("doctor_id", "patient_id", name="uq_patient_notes_doctor_patient"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    doctor_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True)

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
