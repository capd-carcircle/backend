"""
설문 API
- 공통 질문 + AI 맞춤 질문 조회/응답
- AI 질문 생성: ai/ 서버(포트 8001) HTTP 호출로 위임
- 규칙 기반 질문은 백엔드에서 즉시 생성, AI 보완은 백그라운드로 처리
- 설문 완료 시 AI 종합 요약 트리거
"""
import json
import logging
from datetime import datetime, timezone
from typing import List

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.core.auth import get_current_user
from app.core.database import get_db, SessionLocal
from app.models.question import AIQuestion, AIQuestionStatus, AIQuestionType, CommonQuestion
from app.models.record import DailyRecord, RiskLevel
from app.models.survey import SurveyResponse, SurveyChoice, RejectedQPattern
from app.models.user import User, UserRole
from app.schemas.question import AIQuestionResponse
from app.schemas.survey import SurveySubmitRequest, SurveySubmitResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/surveys", tags=["설문"])

AI_SERVER_URL = "http://ai:8001"   # docker-compose 서비스명

# 백그라운드 AI 질문 생성 중인 record_id 추적 (중복 실행 방지)
_ai_in_progress: set = set()

MAX_AI_QUESTIONS  = 5   # 전체 AI 질문 최대 (규칙 기반 + Gemini 합산)
MAX_RULE_QUESTIONS = 3   # 규칙 기반 질문 최대 (Gemini는 나머지 slot 채움)


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

    if record_id in _ai_in_progress:
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
    if current_user.role != UserRole.patient:
        raise HTTPException(status_code=403, detail="환자만 접근할 수 있습니다.")

    record = db.query(DailyRecord).filter(DailyRecord.id == body.record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    if record.patient_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

    saved = 0
    for item in body.responses:
        if item.choice is None and not item.text_answer:
            continue

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
            if item.choice is not None:
                existing.choice = SurveyChoice(item.choice)
            existing.text_answer = item.text_answer or ""
            existing.answered_at = datetime.now(timezone.utc)
        else:
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
    if current_user.role != UserRole.patient:
        raise HTTPException(status_code=403, detail="환자만 접근할 수 있습니다.")

    record = db.query(DailyRecord).filter(DailyRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    if record.patient_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

    responses = (
        db.query(SurveyResponse)
        .filter(SurveyResponse.daily_record_id == record_id)
        .all()
    )
    resp_map = {(r.question_id, r.question_type): r for r in responses}

    common_qs = db.query(CommonQuestion).filter(CommonQuestion.is_active == True).all()
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
        # options JSON 파싱
        options = None
        if q.options:
            try:
                options = json.loads(q.options)
            except Exception:
                options = None
        ai_out.append({
            "question_id":   q.id,
            "question_text": q.question_text,
            "question_type": q.question_type.value if q.question_type else "yes_no",
            "options":       options,
            "reason":        q.reason,
            "choice":        r.choice.value if r and r.choice else None,
            "text_answer":   r.text_answer if r else None,
            "answered":      r is not None,
        })

    total_count    = len(common_out) + len(ai_out)
    answered_count = sum(1 for q in common_out + ai_out if q["answered"])
    ai_pending     = record_id in _ai_in_progress

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
    if current_user.role != UserRole.doctor:
        raise HTTPException(status_code=403, detail="의사만 접근할 수 있습니다.")

    responses = (
        db.query(SurveyResponse)
        .filter(SurveyResponse.daily_record_id == record_id)
        .all()
    )
    resp_map = {(r.question_id, r.question_type): r for r in responses}

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


# ── 규칙 기반 질문 생성 (즉시, 동기) ──────────────────────
def _generate_rule_based(db: Session, record: DailyRecord):
    """KDIGO 5가지 규칙으로 이상 징후 감지 → 즉시 질문 반환"""
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
        if len(generated) >= MAX_RULE_QUESTIONS:
            break
        if rule["key"] in rejected_keys:
            continue
        if rule["check"](recent):
            generated.append({"question_text": rule["question"], "reason": rule["reason"]})

    record_data = {
        "weight":                float(record.weight) if record.weight else None,
        "blood_pressure":        record.blood_pressure,
        "total_ultrafiltration": float(record.total_ultrafiltration) if record.total_ultrafiltration else None,
        "turbid_peritoneal":     record.turbid_peritoneal,
        "fasting_blood_glucose": float(record.fasting_blood_glucose) if record.fasting_blood_glucose else None,
        "memo":                  record.memo,
    }

    return generated, record_data, list(rejected_keys)


# ── AI 서버 백그라운드 질문 보완 ──────────────────────────
def _ai_question_background(record_id: int, patient_id: int, record_data: dict, rejected_keys: list):
    """
    백그라운드에서 ai/ 서버의 /ai-questions/generate 호출
    → 규칙 기반으로 부족한 질문 Gemini로 보완 → DB 저장
    """
    db = SessionLocal()
    try:
        current_count = db.query(AIQuestion).filter(
            AIQuestion.daily_record_id == record_id
        ).count()

        if current_count >= MAX_AI_QUESTIONS:
            logger.info(f"record_id={record_id} 이미 질문 {current_count}개 — AI 서버 생략")
            return

        # ai/ 서버 호출 (동기 httpx)
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{AI_SERVER_URL}/ai-questions/generate",
                json={"record_data": record_data, "rejected_keys": rejected_keys},
            )
            resp.raise_for_status()
            ai_questions = resp.json().get("questions", [])

        added = 0
        for q_data in ai_questions:
            current_count = db.query(AIQuestion).filter(
                AIQuestion.daily_record_id == record_id
            ).count()
            if current_count >= MAX_AI_QUESTIONS:
                break
            q_type_str = q_data.get("question_type", "yes_no")
            try:
                q_type = AIQuestionType(q_type_str)
            except ValueError:
                q_type = AIQuestionType.yes_no
            db.add(AIQuestion(
                daily_record_id=record_id,
                patient_id=patient_id,
                question_text=q_data["question_text"],
                reason=q_data.get("reason"),
                question_type=q_type,
                options=q_data.get("options"),  # 이미 JSON 문자열로 전달됨
            ))
            added += 1

        db.commit()
        logger.info(f"AI 서버 질문 {added}개 저장 완료 (record_id={record_id})")

    except Exception as e:
        logger.warning(f"AI 서버 질문 생성 실패 (record_id={record_id}): {e}")
        db.rollback()
    finally:
        _ai_in_progress.discard(record_id)
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
    return bool(
        records
        and records[0].fasting_blood_glucose
        and float(records[0].fasting_blood_glucose) > 180
    )


# ── POST 설문 완료 + AI 요약 트리거 ──────────────────────
@router.post(
    "/complete/{record_id}",
    summary="설문 완료 — AI 종합 요약 생성 트리거",
)
async def complete_survey(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    환자가 모든 설문 응답 완료 후 호출.
    기록 수치 + 공통질문 응답 + AI 설문 응답을 AI 서버에 전달 → 위험도/요약/EMR 생성 → DB 저장.
    """
    if current_user.role != UserRole.patient:
        raise HTTPException(status_code=403, detail="환자만 접근할 수 있습니다.")

    record = db.query(DailyRecord).filter(DailyRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    if record.patient_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

    # 기록 수치 dict
    record_data = {
        "blood_pressure":        record.blood_pressure,
        "weight":                float(record.weight) if record.weight else None,
        "total_ultrafiltration": float(record.total_ultrafiltration) if record.total_ultrafiltration else None,
        "fasting_blood_glucose": float(record.fasting_blood_glucose) if record.fasting_blood_glucose else None,
        "turbid_peritoneal":     record.turbid_peritoneal,
        "urine_count":           record.urine_count,
        "memo":                  record.memo,
    }

    # 응답 맵 조회
    responses = (
        db.query(SurveyResponse)
        .filter(SurveyResponse.daily_record_id == record_id)
        .all()
    )
    resp_map = {(r.question_id, r.question_type): r for r in responses}

    # 공통 질문 응답 조립
    common_qs = db.query(CommonQuestion).filter(CommonQuestion.is_active == True).all()
    common_qa = []
    for q in common_qs:
        r = resp_map.get((q.id, "common"))
        common_qa.append({
            "question_text": q.question_text,
            "choice":        r.choice.value if r and r.choice else None,
            "text_answer":   r.text_answer if r else None,
        })

    # AI 질문 응답 조립
    ai_qs = (
        db.query(AIQuestion)
        .filter(
            AIQuestion.daily_record_id == record_id,
            AIQuestion.status != AIQuestionStatus.rejected_global,
        )
        .all()
    )
    ai_survey_responses = []
    for q in ai_qs:
        r = resp_map.get((q.id, "ai"))
        if not r:
            continue
        # 응답 텍스트 결정: choice(yes_no) 또는 text_answer(나머지 타입)
        answer = r.text_answer or (r.choice.value if r.choice else None) or "미응답"
        ai_survey_responses.append({
            "question_text": q.question_text,
            "question_type": q.question_type.value if q.question_type else "yes_no",
            "answer":        answer,
        })

    # AI 서버에 요약 요청
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{AI_SERVER_URL}/summary",
                json={
                    "record_data":          record_data,
                    "common_qa":            common_qa,
                    "ai_survey_responses":  ai_survey_responses,
                },
            )
            resp.raise_for_status()
            result = resp.json()
    except Exception as e:
        logger.error(f"AI 서버 요약 요청 실패 (record_id={record_id}): {e}")
        raise HTTPException(status_code=503, detail="AI 서버에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.")

    # DB 저장
    record.risk_level = RiskLevel(result["risk_level"])
    record.ai_summary = result["ai_summary"]
    record.emr_soap   = result["emr_soap"]
    db.commit()

    logger.info(f"설문 완료 요약 저장 (record_id={record_id}, risk={result['risk_level']})")

    return {
        "success":    True,
        "risk_level": result["risk_level"],
        "ai_summary": result["ai_summary"],
    }
