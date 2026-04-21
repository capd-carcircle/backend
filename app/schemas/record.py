from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, field_validator


# ── 교환 기록 ─────────────────────────────────────────────────

class ExchangeRecordCreate(BaseModel):
    session_number: int                         # 1~5
    exchange_time: Optional[str] = None         # "HH:MM"
    drainage_volume: Optional[float] = None     # 배액량 (g)
    infusion_concentration: Optional[float] = None  # 주입액 농도 (%)
    infusion_weight: Optional[float] = None     # 주입액 중량 (g)
    ultrafiltration: Optional[float] = None     # 제수량 (g)


class ExchangeRecordResponse(ExchangeRecordCreate):
    id: int
    daily_record_id: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ── 일일 기록 ─────────────────────────────────────────────────

class DailyRecordCreate(BaseModel):
    record_date: date
    turbid_peritoneal: bool = False             # 복막액 혼탁 여부
    weight: Optional[float] = None             # 체중 (kg)
    blood_pressure: Optional[str] = None       # 혈압 "수축기/이완기"
    urine_count: Optional[int] = None          # 소변 횟수
    total_ultrafiltration: Optional[float] = None  # 제수량 합계 (g)
    fasting_blood_glucose: Optional[float] = None  # 공복혈당 (mg/dL)
    memo: Optional[str] = None
    exchange_records: List[ExchangeRecordCreate] = []

    @field_validator("exchange_records")
    @classmethod
    def validate_sessions(cls, v):
        numbers = [e.session_number for e in v]
        if len(numbers) != len(set(numbers)):
            raise ValueError("회차 번호가 중복됩니다.")
        for n in numbers:
            if n < 1 or n > 5:
                raise ValueError("회차 번호는 1~5 사이여야 합니다.")
        return v


class DailyRecordUpdate(BaseModel):
    """PATCH용 — 모든 필드 선택"""
    turbid_peritoneal: Optional[bool] = None
    weight: Optional[float] = None
    blood_pressure: Optional[str] = None
    urine_count: Optional[int] = None
    total_ultrafiltration: Optional[float] = None
    fasting_blood_glucose: Optional[float] = None
    memo: Optional[str] = None
    exchange_records: Optional[List[ExchangeRecordCreate]] = None


class DailyRecordResponse(BaseModel):
    id: int
    patient_id: int
    record_date: date
    turbid_peritoneal: bool
    weight: Optional[float]
    blood_pressure: Optional[str]
    urine_count: Optional[int]
    total_ultrafiltration: Optional[float]
    fasting_blood_glucose: Optional[float]
    memo: Optional[str]
    status: str
    submitted_at: Optional[datetime]
    exchange_records: List[ExchangeRecordResponse] = []
    created_at: datetime

    model_config = {"from_attributes": True}
