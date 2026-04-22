import json
import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.core.auth import get_current_user
from app.core.database import get_db, SessionLocal
from app.models.question import AIQuestion, AIQuestionStatus, CommonQuestion
from app.models.record import DailyRecord
from app.models.survey import SurveyResponse, SurveyChoice, RejectedQPattern
from app.models.user import User, UserRole
from app.schemas.question import AIQuestionResponse
from app.schemas.survey import SurveySubmitRequest, SurveySubmitResponse
from app.services.ai_service import generate_questions_via_lm_studio
from app.services.rag_service import search_kdigo_context

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/surveys", tags=["설문"])

# LM Studio 백그라운드 처리 중인 record_id 추적 (중복 실행 방지)
_lm_in_progress: set = set()

MAX_AI_QUESTIONS = 3  # AI 질문 최대 개수


# ── GET AI 맞춤 질문 조회 ──────────────────────────────────
@router.get(
    "/ai-questions/{record_id}",
    response_model=List[AIQuestionResponse],
    summary="AI 맞춤 질문 조회",
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

    # 이미 생성 중이면 빈 리스트 (프론트 폴링 대기)
    if record_id in _lm_in_progress:
        return []

    return (
        db.query(AIQuestion)
        .filter(
            AIQuestion.daily_record_id == record_id,
            AIQuestion.status != AIQuestionStatus.rejected_global,
        )
        .all()
    )


# ── POST 설문 응답 저장/수정 (upsert) ─────────────────────
@router.post(
    "/responses",
    response_model=SurveySubmitResponse,
    summary="설문 응답 저장 (upsert — 부분 저장·재답변 모두 가능)",
)
def save_survey_responses(
    body: SurveySubmitRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    - 답변이 있는 항목만 저장 (choice 또는 text_answer)
    - 이미 응답이 있으면 덮어쓰기 (upsert)
    - 부분 저장 가능, 추후 미답변 항목 추가 가능
    """
    if current_user.role != UserRole.patient:
        raise HTTPException(status_code=403, detail="환자만 접근할 수 있습니다.")

    record = db.query(DailyRecord).filter(DailyRecord.id == body.record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    if record.patient_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

    saved = 0
    for item in body.responses:
        # 빈 답변은 건너뜀
        if item.choice is None and not item.text_answer:
            continue

        # 기존 응답 조회
        existing = (
            db.query(SurveyResponse)
            .filter(
                SurveyResponse.daily_record_id == body.record_id,
                SurveyResponse.question_id == item.question_id,
                SurveyResponse.question_type == item.question_type,
            )
            .first()
        )

        if existing:
            # 업데이트
            if item.choice is not None:
                existing.choice = SurveyChoice(item.choice)
            existing.text_answer = item.text_answer or ""
            existing.answered_at = datetime.now(timezone.utc)
        else:
            # 신규 저장
            db.add(SurveyResponse(
                daily_record_id=body.record_id,
                patient_id=current_user.id,
                question_id=item.question_id,
                question_type=item.question_type,
                choice=SurveyChoice(item.choice) if item.choice else None,
                text_answer=item.text_answer or "",
            ))
        saved += 1

    db.commit()
    return SurveySubmitResponse(
        success=True,
        message=f"답변 {saved}개가 저장되었습니다.",
        saved_count=saved,
    )


# ── GET 전체 질문 + 내 답변 조회 (환자용) ──────────────────
@router.get(
    "/my-responses/{record_id}",
    summary="전체 질문 + 내 답변 조회 (환자용)",
)
def get_my_survey_responses(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    공통 질문 + AI 질문 전체를 반환하며, 답변 여부와 관계없이 모두 포함.
    answered=True 인 항목에만 choice/text_answer 값이 있음.
    """
    if current_user.role != UserRole.patient:
        raise HTTPException(status_code=403, detail="환자만 접근할 수 있습니다.")

    record = db.query(DailyRecord).filter(DailyRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    if record.patient_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

    # 기존 응답 맵 { (question_id, question_type): SurveyResponse }
    responses = (
        db.query(SurveyResponse)
        .filter(SurveyResponse.daily_record_id == record_id)
        .all()
    )
    resp_map = {(r.question_id, r.question_type): r for r in responses}

    # 공통 질문 (활성화된 것만)
    common_qs = (
        db.query(CommonQuestion)
        .filter(CommonQuestion.is_active == True)
        .all()
    )
    common_out = []
    for q in common_qs:
        r = resp_map.get((q.id, "common"))
        common_out.append({
            "question_id":   q.id,
            "question_text": q.question_text,
            "reason":        None,
            "choice":        r.choice.value if r and r.choice else None,
            "text_answer":   r.text_answer if r else None,
            "answered":      r is not None,
        })

    # AI 질문 (이 기록용)
    ai_qs = (
        db.query(AIQuestion)
        .filter(
            AIQuestion.daily_record_id == record_id,
            AIQuestion.status != AIQuestionStatus.rejected_global,
        )
        .all()
    )
    ai_out = []
    for q in ai_qs:
        r = resp_map.get((q.id, "ai"))
        ai_out.append({
            "question_id":   q.id,
            "question_text": q.question_text,
            "reason":        q.reason,
            "choice":        r.choice.value if r and r.choice else None,
            "text_answer":   r.text_answer if r else None,
            "answered":      r is not None,
        })

    total_count    = len(common_out) + len(ai_out)
    answered_count = sum(1 for q in common_out + ai_out if q["answered"])

    # AI 질문 생성 중인지 여부
    ai_pending = record_id in _lm_in_progress

    return {
        "common_questions": common_out,
        "ai_questions":     ai_out,
        "total_count":      total_count,
        "answered_count":   answered_count,
        "ai_pending":       ai_pending,
    }


# ── GET 전체 질문 + 답변 조회 (의사용) ────────────────────
@router.get(
    "/responses/{record_id}",
    summary="기록별 전체 질문 + 답변 조회 (의사 전용)",
)
def get_survey_responses(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    의사용: 공통 질문 + AI 질문 전체 반환 (미답변 포함)
    """
    if current_user.role != UserRole.doctor:
        raise HTTPException(status_code=403, detail="의사만 접근할 수 있습니다.")

    # 기존 응답 맵
    responses = (
        db.query(SurveyResponse)
        .filter(SurveyResponse.daily_record_id == record_id)
        .all()
    )
    resp_map = {(r.question_id, r.question_type): r for r in responses}

    # 공통 질문
    common_qs = db.query(CommonQuestion).filter(CommonQuestion.is_active == True).all()
    result = []
    for q in common_qs:
        r = resp_map.get((q.id, "common"))
        result.append({
            "question_type": "common",
            "question_text": q.question_text,
            "reason":        None,
            "choice":        r.choice.value if r and r.choice else None,
            "text_answer":   r.text_answer if r else None,
            "answered":      r is not None,
            "answered_at":   r.answered_at.isoformat() if r else None,
        })

    # AI 질문
    ai_qs = (
        db.query(AIQuestion)
        .filter(
            AIQuestion.daily_record_id == record_id,
            AIQuestion.status != AIQuestionStatus.rejected_global,
        )
        .all()
    )
    for q in ai_qs:
        r = resp_map.get((q.id, "ai"))
        result.append({
            "question_type": "ai",
            "question_text": q.question_text,
            "reason":        q.reason,
            "choice":        r.choice.value if r and r.choice else None,
            "text_answer":   r.text_answer if r else None,
            "answered":      r is not None,
            "answered_at":   r.answered_at.isoformat() if r else None,
        })

    return result


# ── 규칙 기반 질문 생성 (동기, 즉시 반환용) ──────────────────
def _generate_rule_based(db: Session, record: DailyRecord):
    """KDIGO 규칙 5가지로 이상 징후 탐지 → 즉시 반환 가능한 질문 생성"""
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
        if len(generated) >= MAX_AI_QUESTIONS:
            break
        if rule["key"] in rejected_keys:
            continue
        if rule["check"](recent):
            generated.append({"question_text": rule["question"], "reason": rule["reason"]})

    record_data = {
        "weight": float(record.weight) if record.weight else None,
        "blood_pressure": record.blood_pressure,
        "total_uf": float(record.total_ultrafiltration) if record.total_ultrafiltration else None,
        "turbid": record.turbid_peritoneal,
        "blood_sugar": float(record.fasting_blood_glucose) if record.fasting_blood_glucose else None,
        "memo": record.memo,
        "recent_uf_7d": [float(r.total_ultrafiltration) for r in recent if r.total_ultrafiltration],
    }

    return generated, record_data, rejected_keys


# ── LM Studio 백그라운드 질문 생성 ──────────────────────────
def _lm_question_background(
    record_id: int,
    patient_id: int,
    record_data: dict,
    rejected_keys: list,
):
    """백그라운드에서 RAG + LM Studio로 AI 질문 보완 후 DB 저장"""
    db = SessionLocal()
    try:
        current_count = db.query(AIQuestion).filter(
            AIQuestion.daily_record_id == record_id
        ).count()

        if current_count >= MAX_AI_QUESTIONS:
            logger.info(f"record_id={record_id} 이미 질문 {current_count}개 — LM Studio 생략")
            return

        kdigo_context = search_kdigo_context(record_data, db)
        lm_questions = generate_questions_via_lm_studio(
            record_data, rejected_keys, kdigo_context=kdigo_context
        )

        added = 0
        for q_data in lm_questions:
            current_count = db.query(AIQuestion).filter(
                AIQuestion.daily_record_id == record_id
            ).count()
            if current_count >= MAX_AI_QUESTIONS:
                break
            db.add(AIQuestion(
                daily_record_id=record_id,
                patient_id=patient_id,
                question_text=q_data["question_text"],
                reason=q_data.get("reason"),
            ))
            added += 1

        db.commit()
        logger.info(f"백그라운드 LM Studio 질문 {added}개 저장 완료 (record_id={record_id})")

    except Exception as e:
        logger.warning(f"백그라운드 LM Studio 질문 생성 실패 (record_id={record_id}): {e}")
        db.rollback()
    finally:
        _lm_in_progress.discard(record_id)
        db.close()


# ── 규칙 체크 함수들 ──────────────────────────────────────
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
