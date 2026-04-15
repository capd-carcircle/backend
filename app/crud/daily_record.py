from datetime import datetime, timezone
from typing import List

from sqlalchemy.orm import Session, joinedload

from app.models.record import DailyRecord, ExchangeRecord, RecordStatus
from app.schemas.record import DailyRecordCreate


def create_daily_record(
    db: Session, patient_id: int, data: DailyRecordCreate
) -> DailyRecord:
    """일일 기록 + 교환 기록 함께 저장"""
    record = DailyRecord(
        patient_id=patient_id,
        record_date=data.record_date,
        turbid_peritoneal=data.turbid_peritoneal,
        weight=data.weight,
        blood_pressure=data.blood_pressure,
        urine_count=data.urine_count,
        total_ultrafiltration=data.total_ultrafiltration,
        fasting_blood_glucose=data.fasting_blood_glucose,
        memo=data.memo,
        status=RecordStatus.submitted,
        submitted_at=datetime.now(timezone.utc),
    )
    db.add(record)
    db.flush()  # PK 획득

    for ex in data.exchange_records:
        db.add(ExchangeRecord(
            daily_record_id=record.id,
            session_number=ex.session_number,
            exchange_time=ex.exchange_time,
            drainage_volume=ex.drainage_volume,
            infusion_concentration=ex.infusion_concentration,
            infusion_weight=ex.infusion_weight,
            ultrafiltration=ex.ultrafiltration,
        ))

    db.commit()
    db.refresh(record)
    return record


def get_patient_records(db: Session, patient_id: int) -> List[DailyRecord]:
    """환자의 기록 목록 (최신순)"""
    return (
        db.query(DailyRecord)
        .options(joinedload(DailyRecord.exchange_records))
        .filter(DailyRecord.patient_id == patient_id)
        .order_by(DailyRecord.record_date.desc())
        .all()
    )


def get_record_by_id(db: Session, record_id: int) -> DailyRecord | None:
    """특정 기록 단건 조회 (교환 기록 포함)"""
    return (
        db.query(DailyRecord)
        .options(joinedload(DailyRecord.exchange_records))
        .filter(DailyRecord.id == record_id)
        .first()
    )
