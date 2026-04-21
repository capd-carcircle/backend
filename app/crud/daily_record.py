from datetime import datetime, timezone
from typing import List

from sqlalchemy.orm import Session, joinedload

from app.models.question import AIQuestion
from app.models.record import DailyRecord, ExchangeRecord, RecordStatus
from app.models.survey import SurveyResponse
from app.schemas.record import DailyRecordCreate, DailyRecordUpdate


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


def update_daily_record(
    db: Session, record: DailyRecord, data: DailyRecordUpdate
) -> DailyRecord:
    """submitted 상태 기록 수정 (교환 기록 전체 교체)"""
    if data.turbid_peritoneal is not None:
        record.turbid_peritoneal = data.turbid_peritoneal
    if data.weight is not None:
        record.weight = data.weight
    if data.blood_pressure is not None:
        record.blood_pressure = data.blood_pressure
    if data.urine_count is not None:
        record.urine_count = data.urine_count
    if data.total_ultrafiltration is not None:
        record.total_ultrafiltration = data.total_ultrafiltration
    if data.fasting_blood_glucose is not None:
        record.fasting_blood_glucose = data.fasting_blood_glucose
    if data.memo is not None:
        record.memo = data.memo
    record.updated_at = datetime.now(timezone.utc)

    if data.exchange_records is not None:
        # 기존 교환 기록 삭제 후 재생성
        db.query(ExchangeRecord).filter(
            ExchangeRecord.daily_record_id == record.id
        ).delete()
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


def delete_daily_record(db: Session, record: DailyRecord) -> None:
    """기록 삭제 — FK 참조 테이블 순서대로 삭제"""
    rid = record.id
    # 1) 설문 응답 삭제 (survey_responses.daily_record_id)
    db.query(SurveyResponse).filter(SurveyResponse.daily_record_id == rid).delete()
    # 2) AI 맞춤 질문 삭제 (ai_questions.daily_record_id)
    db.query(AIQuestion).filter(AIQuestion.daily_record_id == rid).delete()
    # 3) 교환 기록 삭제 (exchange_records — relationship cascade 있지만 명시적으로)
    db.query(ExchangeRecord).filter(ExchangeRecord.daily_record_id == rid).delete()
    # 4) 본체 삭제
    db.delete(record)
    db.commit()


def get_record_by_id(db: Session, record_id: int) -> DailyRecord | None:
    """특정 기록 단건 조회 (교환 기록 포함)"""
    return (
        db.query(DailyRecord)
        .options(joinedload(DailyRecord.exchange_records))
        .filter(DailyRecord.id == record_id)
        .first()
    )
