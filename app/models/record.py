import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Enum, ForeignKey,
    Integer, Numeric, String, Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class RecordStatus(str, enum.Enum):
    submitted = "submitted"   # 환자 제출
    reviewed  = "reviewed"    # 의사 검토 완료
    rejected  = "rejected"    # 의사 반려


class DailyRecord(Base):
    __tablename__ = "daily_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True
    )
    record_date: Mapped[datetime] = mapped_column(Date, nullable=False)

    # ── 기타 기록 ──────────────────────────────────────────────
    turbid_peritoneal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    weight: Mapped[Optional[float]] = mapped_column(Numeric(5, 1), nullable=True)
    blood_pressure: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # "120/80"
    urine_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_ultrafiltration: Mapped[Optional[float]] = mapped_column(Numeric(8, 1), nullable=True)
    fasting_blood_glucose: Mapped[Optional[float]] = mapped_column(Numeric(6, 1), nullable=True)
    memo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── 상태 ───────────────────────────────────────────────────
    status: Mapped[RecordStatus] = mapped_column(
        Enum(RecordStatus, name="record_status_enum"),
        default=RecordStatus.submitted,
        nullable=False,
    )
    submitted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_by: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True
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

    # ── 관계 ───────────────────────────────────────────────────
    exchange_records = relationship(
        "ExchangeRecord",
        back_populates="daily_record",
        cascade="all, delete-orphan",
        order_by="ExchangeRecord.session_number",
    )


class ExchangeRecord(Base):
    __tablename__ = "exchange_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    daily_record_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("daily_records.id"), nullable=False, index=True
    )
    session_number: Mapped[int] = mapped_column(Integer, nullable=False)  # 1~5

    exchange_time: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)   # "HH:MM"
    drainage_volume: Mapped[Optional[float]] = mapped_column(Numeric(8, 1), nullable=True)
    infusion_concentration: Mapped[Optional[float]] = mapped_column(Numeric(4, 1), nullable=True)
    infusion_weight: Mapped[Optional[float]] = mapped_column(Numeric(8, 1), nullable=True)
    ultrafiltration: Mapped[Optional[float]] = mapped_column(Numeric(8, 1), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # ── 관계 ───────────────────────────────────────────────────
    daily_record = relationship("DailyRecord", back_populates="exchange_records")
