"""병원 관련 모델: hospitals, doctor_licenses, doctor_profiles"""
import enum
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Hospital(Base):
    __tablename__ = "hospitals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class DoctorLicense(Base):
    """의사 자격번호 시드 테이블 — 신규 가입 시 검증용"""
    __tablename__ = "doctor_licenses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)          # 실명
    birth_date: Mapped[str] = mapped_column(String(10), nullable=False)      # YYYY-MM-DD
    license_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    hospital_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("hospitals.id", ondelete="SET NULL"), nullable=True
    )
    is_registered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # 이미 가입했으면 True


class DoctorProfile(Base):
    """의사 추가 정보 (1:1 with users)"""
    __tablename__ = "doctor_profiles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    birth_date: Mapped[str] = mapped_column(String(10), nullable=False)      # YYYY-MM-DD
    license_number: Mapped[str] = mapped_column(String(50), nullable=False)
    hospital_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("hospitals.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
