import json
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.question import AIQuestion, AIQuestionStatus, CommonQuestion
from app.models.record import DailyRecord
from app.models.survey import SurveyResponse, RejectedQPattern
from app.models.user import User, UserRole
from app.schemas.question import AIQuestionResponse
from app.schemas.survey import SurveySubmitRequest, SurveySubmitResponse
from app.services.ai_service import generate_questions_via_lm_studio
from app.services.rag_service import search_kdigo_context

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/surveys", tags=["설문"])


# ── GET AI 맞춤 질문 (없으면 생성) ────────────────────────
@router.get(
    "/ai-questions/{record_id}",
    response_model=List[AIQuestionResponse],
    summary="AI 맞춤 질문 조회 (없으면 자동 생성)",
)
def get_ai_questions(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.patient:
        raise HTTPException(status_code=403, detail="환자만 접근할 수 있습니다.")

    record = db.query(DailyRecord).filter(DailyRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    if record.patient_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

    # 기존 질문 조회 (전체 환자 거절 제외)
    existing = (
        db.query(AIQuestion)
        .filter(
            AIQuestion.daily_record_id == record_id,
            AIQuestion.status != AIQuestionStatus.rejected_global,
        )
        .all()
    )
    if existing:
        return existing

    # 신규 생성
    questions = _generate_ai_questions(db, record)
    for q_data in questions:
        ai_q = AIQuestion(
            daily_record_id=record.id,
            patient_id=current_user.id,
            question_text=q_data["question_text"],
            reason=q_data.get("reason"),
        )
        db.add(ai_q)
    db.commit()

    return (
        db.query(AIQuestion)
        .filter(AIQuestion.daily_record_id == record_id)
        .all()
    )


# ── POST 설문 응답 제출 ────────────────────────────────────
@router.post(
    "/responses",
    response_model=SurveySubmitResponse,
    summary="설문 응답 제출",
)
def submit_survey(
    body: SurveySubmitRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.patient:
        raise HTTPException(status_code=403, detail="환자만 접근할 수 있습니다.")

    record = db.query(DailyRecord).filter(DailyRecord.id == body.record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    if record.patient_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

    # 중복 제출 방지
    already = (
        db.query(SurveyResponse)
        .filter(SurveyResponse.daily_record_id == body.record_id)
        .count()
    )
    if already > 0:
        raise HTTPException(status_code=409, detail="이미 설문이 제출된 기록입니다.")

    saved = 0
    for item in body.responses:
        if item.choice is None and not item.text_answer:
            continue
        db.add(SurveyResponse(
            daily_record_id=body.record_id,
            patient_id=current_user.id,
            question_id=item.question_id,
            question_type=item.question_type,
            choice=item.choice,
            text_answer=item.text_answer or "",
        ))
        saved += 1

    db.commit()
    return SurveySubmitResponse(success=True, message="설문이 제출되었습니다.", saved_count=saved)


# ── GET 설문 응답 조회 (의사용) ────────────────────────────
@router.get(
    "/responses/{record_id}",
    summary="기록별 설문 응답 조회 (의사 전용)",
)
def get_survey_responses(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.doctor:
        raise HTTPException(status_code=403, detail="의사만 접근할 수 있습니다.")

    responses = (
        db.query(SurveyResponse)
        .filter(SurveyResponse.daily_record_id == record_id)
        .all()
    )

    result = []
    for r in responses:
        if r.question_type == "common":
            q = db.query(CommonQuestion).filter(CommonQuestion.id == r.question_id).first()
            q_text = q.question_text if q else "(삭제된 질문)"
            q_reason = None
        else:
            q = db.query(AIQuestion).filter(AIQuestion.id == r.question_id).first()
            q_text = q.question_text if q else "(삭제된 질문)"
            q_reason = q.reason if q else None

        result.append({
            "response_id": r.id,
            "question_type": r.question_type,
            "question_text": q_text,
            "reason": q_reason,
            "choice": r.choice,
            "text_answer": r.text_answer,
            "answered_at": r.answered_at.isoformat() if r.answered_at else None,
        })

    return result


# ── AI 질문 생성 (규칙 기반 + LM Studio) ──────────────────
def _generate_ai_questions(db: Session, record: DailyRecord, max_q: int = 3) -> list:
    """KDIGO 기반 규칙으로 이상 징후 탐지 → 맞춤 질문 생성, 부족하면 LM Studio 보완"""
    recent = (
        db.query(DailyRecord)
        .filter(
            DailyRecord.patient_id == record.patient_id,
            DailyRecord.id <= record.id,
        )
        .order_by(desc(DailyRecord.record_date))
        .limit(7)
        .all()
    )

    # 거절된 패턴
    rejected_keys = {
        r.pattern for r in db.query(RejectedQPattern).filter(
            (RejectedQPattern.patient_id == record.patient_id)
            | (RejectedQPattern.patient_id.is_(None))
        ).all()
    }

    RULES = [
        {
            "key": "uf_decrease",
            "check": _check_uf_decrease,
            "question": "지난 3일 간 한외여과량이 감소하고 있습니다. 수분 섭취가 평소보다 많았나요?",
            "reason": "한외여과 감소 추세 탐지",
        },
        {
            "key": "high_bp",
            "check": _check_high_bp,
            "question": "오늘 혈압이 평소보다 높게 측정되었습니다. 두통이나 어지러움이 있었나요?",
            "reason": "혈압 이상 감지 (KDIGO 기준)",
        },
        {
            "key": "cloudy",
            "check": _check_cloudy,
            "question": "투석액이 혼탁하게 나왔습니다. 복통이나 발열 증상이 있었나요?",
            "reason": "복막염 의심 — 혼탁 투석액 감지",
        },
        {
            "key": "weight_up",
            "check": _check_weight_increase,
            "question": "체중이 전날보다 증가했습니다. 발이나 손이 붓는 느낌이 있었나요?",
            "reason": "체중 증가 — 수분 과부하 의심",
        },
        {
            "key": "high_glucose",
            "check": _check_blood_sugar,
            "question": "공복 혈당이 높게 측정되었습니다. 어제 식사나 간식 섭취가 평소와 달랐나요?",
            "reason": "공복 혈당 이상 (당뇨 관리 KDIGO 기준)",
        },
    ]

    generated = []
    for rule in RULES:
        if len(generated) >= max_q:
            break
        if rule["key"] in rejected_keys:
            continue
        if rule["check"](recent):
            generated.append({"question_text": rule["question"], "reason": rule["reason"]})

    # LM Studio 보완 (규칙으로 부족할 때)
    if len(generated) < max_q:
        record_data = {
            "weight": float(record.weight) if record.weight else None,
            "blood_pressure": record.blood_pressure,
            "total_uf": float(record.total_ultrafiltration) if record.total_ultrafiltration else None,
            "turbid": record.turbid_peritoneal,
            "blood_sugar": float(record.fasting_blood_glucose) if record.fasting_blood_glucose else None,
            "memo": record.memo,
            "recent_uf_7d": [float(r.total_ultrafiltration) for r in recent if r.total_ultrafiltration],
        }
        # RAG: 환자 기록과 관련된 KDIGO 문단 검색
        kdigo_context = search_kdigo_context(record_data, db)
        lm_questions = generate_questions_via_lm_studio(
            record_data, list(rejected_keys), kdigo_context=kdigo_context
        )
        for q in lm_questions:
            if len(generated) >= max_q:
                break
            generated.append(q)

    return generated


def _check_uf_decrease(records):
    ufs = [r.total_ultrafiltration for r in records if r.total_ultrafiltration is not None]
    return len(ufs) >= 3 and ufs[0] < ufs[1] < ufs[2]


def _check_high_bp(records):
    if not records or not records[0].blood_pressure:
        return False
    try:
        return int(records[0].blood_pressure.split("/")[0]) > 140
    except Exception:
        return False


def _check_cloudy(records):
    return bool(records and records[0].turbid_peritoneal)


def _check_weight_increase(records):
    weights = [r.weight for r in records if r.weight is not None]
    return len(weights) >= 2 and (float(weights[0]) - float(weights[1])) >= 1.0


def _check_blood_sugar(records):
    return bool(records and records[0].fasting_blood_glucose
                and float(records[0].fasting_blood_glucose) > 180)
