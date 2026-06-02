"""
설문 API
- 공통 질문 + AI 맞춤 질문 조회/응답
- 새 SSE 흐름 (v3):
    1. POST /surveys/{id}/common  — 공통질문 답변 저장
    2. GET  /surveys/{id}/ai-questions/stream  — SSE로 AI 질문 스트리밍
    3. POST /surveys/{id}/ai  — AI 질문 답변 저장 + 요약 백그라운드 트리거
- 기존 엔드포인트 유지 (하위 호환)
"""
import json
import logging
from datetime import datetime, timezone
from typing import AsyncGenerator, List

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import get_db, SessionLocal
from app.models.patient_assignment import PatientDoctorAssignment
from app.models.question import (
    AIQuestion, AIQuestionStatus, AIQuestionType,
    CommonQuestion, QuestionPatientAssignment,
)
from app.models.patient_note import PatientNote
from app.models.record import DailyRecord, ExchangeRecord
from app.models.survey import SurveyResponse, SurveyChoice
from app.models.user import User, UserRole
from app.schemas.question import AIQuestionResponse
from app.schemas.survey import SurveySubmitRequest, SurveySubmitResponse
from app.services.ai_background import (
    _summary_in_progress,
    summary_background,
    compute_historical_context,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/surveys", tags=["설문"])

AI_SERVER_URL = settings.AI_SERVICE_URL
MAX_AI_QUESTIONS = 5   # AI 질문 최대 개수


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
    if record.risk_level is not None:
        raise HTTPException(status_code=409, detail="이미 제출된 설문입니다. 수정할 수 없습니다.")

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

    from sqlalchemy import or_
    _assigned_q_ids = (
        db.query(QuestionPatientAssignment.question_id)
        .filter(QuestionPatientAssignment.patient_id == current_user.id)
        .subquery()
    )
    common_qs = (
        db.query(CommonQuestion)
        .filter(
            CommonQuestion.is_active == True,
            or_(
                CommonQuestion.target_all_patients == True,
                CommonQuestion.id.in_(_assigned_q_ids),
            ),
        )
        .order_by(CommonQuestion.created_at.asc())
        .all()
    )
    common_out = []
    for q in common_qs:
        r = resp_map.get((q.id, "common"))
        # options JSON 파싱
        c_options = None
        if q.options:
            try:
                c_options = json.loads(q.options)
            except Exception:
                c_options = None
        common_out.append({
            "question_id":   q.id,
            "question_text": q.question_text,
            "question_type": q.question_type.value if q.question_type else "yes_no",
            "options":       c_options,
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

    total_count       = len(common_out) + len(ai_out)
    answered_count    = sum(1 for q in common_out + ai_out if q["answered"])
    survey_completed  = record.risk_level is not None or record_id in _summary_in_progress

    return {
        "common_questions": common_out,
        "ai_questions":     ai_out,
        "total_count":      total_count,
        "answered_count":   answered_count,
        "ai_pending":       False,   # SSE 방식으로 전환, 폴링 필요 없음
        "survey_completed": survey_completed,
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

    record_obj = db.query(DailyRecord).filter(DailyRecord.id == record_id).first()
    if not record_obj:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")

    # 담당 의사인지 확인 (현재 또는 과거 담당 모두 허용)
    has_access = db.query(PatientDoctorAssignment).filter(
        PatientDoctorAssignment.doctor_id == current_user.id,
        PatientDoctorAssignment.patient_id == record_obj.patient_id,
    ).first()
    if not has_access:
        raise HTTPException(status_code=403, detail="해당 환자의 담당 의사가 아닙니다.")

    responses = (
        db.query(SurveyResponse)
        .filter(SurveyResponse.daily_record_id == record_id)
        .all()
    )
    resp_map = {(r.question_id, r.question_type): r for r in responses}

    from sqlalchemy import or_
    _pid = record_obj.patient_id
    _assigned_dr = (
        db.query(QuestionPatientAssignment.question_id)
        .filter(QuestionPatientAssignment.patient_id == _pid)
        .subquery()
    )

    _cq = db.query(CommonQuestion).filter(CommonQuestion.is_active == True)
    _cq = _cq.filter(
        or_(
            CommonQuestion.target_all_patients == True,
            CommonQuestion.id.in_(_assigned_dr),
        )
    )
    common_qs = _cq.order_by(CommonQuestion.created_at.asc()).all()

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


# ── POST 설문 완료 + AI 요약 트리거 (백그라운드) ──────────
@router.post(
    "/complete/{record_id}",
    summary="설문 완료 — AI 종합 요약 백그라운드 생성 트리거",
)
def complete_survey(
    record_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    환자가 모든 설문 응답 완료 후 호출.
    AI 요약은 백그라운드에서 생성되며, 호출 즉시 완료 응답 반환.
    중복 호출 방지: 이미 요약이 있거나 생성 중이면 409 반환.
    """
    if current_user.role != UserRole.patient:
        raise HTTPException(status_code=403, detail="환자만 접근할 수 있습니다.")

    record = db.query(DailyRecord).filter(DailyRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    if record.patient_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

    # 중복 제출 방지: 이미 요약 생성 완료 또는 진행 중
    if record.risk_level is not None:
        raise HTTPException(status_code=409, detail="이미 제출된 설문입니다.")
    if record_id in _summary_in_progress:
        raise HTTPException(status_code=409, detail="AI 요약이 이미 생성 중입니다.")

    # 오늘 기록 exchange_records 조회
    from app.models.record import ExchangeRecord
    survey_exchanges = (
        db.query(ExchangeRecord)
        .filter(ExchangeRecord.daily_record_id == record_id)
        .order_by(ExchangeRecord.session_number)
        .all()
    )
    survey_exchange_list = [
        {
            "session_number":         ex.session_number,
            "exchange_time":          ex.exchange_time,
            "drainage_volume":        float(ex.drainage_volume) if ex.drainage_volume is not None else None,
            "infusion_concentration": float(ex.infusion_concentration) if ex.infusion_concentration is not None else None,
            "infusion_weight":        float(ex.infusion_weight) if ex.infusion_weight is not None else None,
            "ultrafiltration":        float(ex.ultrafiltration) if ex.ultrafiltration is not None else None,
        }
        for ex in survey_exchanges
    ]

    # 응답 데이터 수집 (백그라운드 함수에 넘길 snapshot, exchange_records 포함)
    record_data = {
        "date":                  str(record.record_date),
        "blood_pressure":        record.blood_pressure,
        "weight":                float(record.weight) if record.weight else None,
        "total_ultrafiltration": float(record.total_ultrafiltration) if record.total_ultrafiltration else None,
        "fasting_blood_glucose": float(record.fasting_blood_glucose) if record.fasting_blood_glucose else None,
        "turbid_peritoneal":     record.turbid_peritoneal,
        "urine_count":           record.urine_count,
        "memo":                  record.memo,
        "exchange_records":      survey_exchange_list,
    }

    from sqlalchemy import or_
    _assigned_complete = (
        db.query(QuestionPatientAssignment.question_id)
        .filter(QuestionPatientAssignment.patient_id == current_user.id)
        .subquery()
    )
    common_qs = (
        db.query(CommonQuestion)
        .filter(
            CommonQuestion.is_active == True,
            or_(
                CommonQuestion.target_all_patients == True,
                CommonQuestion.id.in_(_assigned_complete),
            ),
        )
        .order_by(CommonQuestion.created_at.asc())
        .all()
    )
    responses = (
        db.query(SurveyResponse)
        .filter(SurveyResponse.daily_record_id == record_id)
        .all()
    )
    resp_map = {(r.question_id, r.question_type): r for r in responses}

    common_qa = []
    for q in common_qs:
        r = resp_map.get((q.id, "common"))
        common_qa.append({
            "question_text": q.question_text,
            "choice":        r.choice.value if r and r.choice else None,
            "text_answer":   r.text_answer if r else None,
        })

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
        answer = r.text_answer or (r.choice.value if r.choice else None) or "미응답"
        ai_survey_responses.append({
            "question_text": q.question_text,
            "question_type": q.question_type.value if q.question_type else "yes_no",
            "answer":        answer,
        })

    hist_result = compute_historical_context(db, current_user.id, record_id)
    historical_context = hist_result["context"]
    historical_records = hist_result["historical_records"]

    patient_user = db.query(User).filter(User.id == current_user.id).first()
    doctor_note_row = (
        db.query(PatientNote)
        .filter(PatientNote.patient_id == current_user.id)
        .order_by(PatientNote.updated_at.desc())
        .first()
    )
    patient_profile = {
        "self_memo":   patient_user.self_memo if patient_user and patient_user.self_memo else None,
        "doctor_note": doctor_note_row.content if doctor_note_row and doctor_note_row.content else None,
    }

    # 백그라운드 요약 생성 트리거
    _summary_in_progress.add(record_id)
    background_tasks.add_task(
        summary_background,
        record_id=record_id,
        record_data=record_data,
        common_qa=common_qa,
        ai_survey_responses=ai_survey_responses,
        historical_context=historical_context,
        patient_profile=patient_profile,
        historical_records=historical_records,
    )

    logger.info(f"설문 완료 — AI 요약 백그라운드 트리거 (record_id={record_id})")
    return {"success": True}


# ════════════════════════════════════════════════════════════════════
# 신규 SSE 흐름 엔드포인트 (v3)
# ════════════════════════════════════════════════════════════════════

# ── 공통질문 답변 저장 ───────────────────────────────────────────────
@router.post(
    "/{record_id}/common",
    summary="공통질문 답변 저장 (SSE 흐름 Step 1)",
)
def submit_common_survey(
    record_id: int,
    body: SurveySubmitRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    공통질문 답변만 저장. 완료 후 프론트는 /ai-questions/stream 으로 이동.
    body.record_id와 path record_id를 모두 허용 (body.record_id 우선).
    """
    if current_user.role != UserRole.patient:
        raise HTTPException(status_code=403, detail="환자만 접근할 수 있습니다.")

    effective_record_id = body.record_id if body.record_id else record_id
    record = db.query(DailyRecord).filter(DailyRecord.id == effective_record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    if record.patient_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")
    if record.risk_level is not None:
        raise HTTPException(status_code=409, detail="이미 제출된 설문입니다. 수정할 수 없습니다.")

    saved = 0
    for item in body.responses:
        if item.choice is None and not item.text_answer:
            continue
        existing = (
            db.query(SurveyResponse)
            .filter(
                SurveyResponse.daily_record_id == effective_record_id,
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
                daily_record_id=effective_record_id,
                patient_id=current_user.id,
                question_id=item.question_id,
                question_type=item.question_type,
                choice=SurveyChoice(item.choice) if item.choice else None,
                text_answer=item.text_answer or "",
            ))
        saved += 1

    db.commit()
    logger.info(f"공통질문 답변 {saved}개 저장 (record_id={effective_record_id})")
    return {"success": True, "saved_count": saved}


# ── AI 질문 SSE 스트리밍 ────────────────────────────────────────────
@router.get(
    "/{record_id}/ai-questions/stream",
    summary="AI 맞춤 질문 SSE 스트리밍 (SSE 흐름 Step 2)",
)
async def stream_ai_questions(
    record_id: int,
    token: str = Query(..., description="JWT access token (EventSource는 헤더 미지원)"),
    db: Session = Depends(get_db),
):
    """
    ai 서버의 /ai-questions/generate-stream을 SSE 프록시.
    질문이 생성될 때마다 즉시 DB 저장 + SSE 이벤트 전송.
    이미 AI 질문이 있으면 기존 질문을 즉시 스트리밍 후 done.

    인증: EventSource가 Authorization 헤더를 지원하지 않으므로 쿼리파라미터 token 사용.
    """
    from app.core.auth import decode_access_token

    # 토큰 검증 (실패 시 HTTPException → FastAPI가 401 반환)
    payload = decode_access_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")

    current_user = db.query(User).filter(User.id == int(user_id)).first()
    if not current_user or not current_user.is_active:
        raise HTTPException(status_code=401, detail="사용자를 찾을 수 없습니다.")
    if current_user.role != UserRole.patient:
        raise HTTPException(status_code=403, detail="환자만 접근할 수 있습니다.")

    record = db.query(DailyRecord).filter(DailyRecord.id == record_id).first()
    if not record or record.patient_id != current_user.id:
        async def not_found():
            yield "event: error\ndata: {\"message\": \"기록을 찾을 수 없습니다.\"}\n\n"
        return StreamingResponse(not_found(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # 이미 AI 질문이 있으면 기존 질문 즉시 스트리밍
    existing_questions = (
        db.query(AIQuestion)
        .filter(
            AIQuestion.daily_record_id == record_id,
            AIQuestion.status != AIQuestionStatus.rejected_global,
        )
        .all()
    )
    if existing_questions:
        async def replay_existing():
            for idx, q in enumerate(existing_questions):
                opts = None
                if q.options:
                    try:
                        opts = json.loads(q.options)
                    except Exception:
                        opts = None
                data = json.dumps({
                    "question_id":   q.id,
                    "question_text": q.question_text,
                    "question_type": q.question_type.value if q.question_type else "yes_no",
                    "options":       opts,
                    "reason":        q.reason,
                }, ensure_ascii=False)
                yield f"id: {idx}\ndata: {data}\n\n"
            yield "event: done\ndata: {}\n\n"
        return StreamingResponse(replay_existing(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                          "Connection": "keep-alive"})

    # 신규 생성: 공통질문 답변 수집
    from sqlalchemy import or_
    _assigned_q_ids = (
        db.query(QuestionPatientAssignment.question_id)
        .filter(QuestionPatientAssignment.patient_id == current_user.id)
        .subquery()
    )
    common_qs = (
        db.query(CommonQuestion)
        .filter(
            CommonQuestion.is_active == True,
            or_(
                CommonQuestion.target_all_patients == True,
                CommonQuestion.id.in_(_assigned_q_ids),
            ),
        )
        .all()
    )
    responses = (
        db.query(SurveyResponse)
        .filter(SurveyResponse.daily_record_id == record_id)
        .all()
    )
    resp_map = {(r.question_id, r.question_type): r for r in responses}
    common_question_responses = []
    for q in common_qs:
        r = resp_map.get((q.id, "common"))
        if r:
            answer = r.text_answer or (r.choice.value if r.choice else "미응답")
            common_question_responses.append({
                "question_text": q.question_text,
                "answer":        answer,
            })

    # 기록 데이터 구성
    exchanges = (
        db.query(ExchangeRecord)
        .filter(ExchangeRecord.daily_record_id == record_id)
        .order_by(ExchangeRecord.session_number)
        .all()
    )
    exchange_list = [
        {
            "session_number":         ex.session_number,
            "exchange_time":          ex.exchange_time,
            "drainage_volume":        float(ex.drainage_volume) if ex.drainage_volume is not None else None,
            "infusion_concentration": float(ex.infusion_concentration) if ex.infusion_concentration is not None else None,
            "infusion_weight":        float(ex.infusion_weight) if ex.infusion_weight is not None else None,
            "ultrafiltration":        float(ex.ultrafiltration) if ex.ultrafiltration is not None else None,
        }
        for ex in exchanges
    ]
    record_data = {
        "date":                  str(record.record_date),
        "weight":                float(record.weight) if record.weight else None,
        "blood_pressure":        record.blood_pressure,
        "total_ultrafiltration": float(record.total_ultrafiltration) if record.total_ultrafiltration else None,
        "turbid_peritoneal":     record.turbid_peritoneal,
        "fasting_blood_glucose": float(record.fasting_blood_glucose) if record.fasting_blood_glucose else None,
        "urine_count":           record.urine_count,
        "memo":                  record.memo,
        "exchange_records":      exchange_list,
    }

    # 환자 프로필
    doctor_note_row = (
        db.query(PatientNote)
        .filter(PatientNote.patient_id == current_user.id)
        .order_by(PatientNote.updated_at.desc())
        .first()
    )
    patient_profile = {
        "self_memo":   current_user.self_memo if current_user.self_memo else None,
        "doctor_note": doctor_note_row.content if doctor_note_row else None,
    }

    # 과거 기록
    hist_result        = compute_historical_context(db, current_user.id, record_id)
    historical_records = hist_result["historical_records"]

    # ai 서버 SSE 프록시
    saved_record_id  = record_id
    saved_patient_id = current_user.id

    async def proxy_sse():
        db_proxy = SessionLocal()
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    f"{AI_SERVER_URL}/ai-questions/generate-stream",
                    json={
                        "record_data":               record_data,
                        "patient_profile":           patient_profile,
                        "historical_records":        historical_records,
                        "common_question_responses": common_question_responses,
                    },
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    if resp.status_code >= 400:
                        error_data = json.dumps({"message": f"AI 서버 오류 ({resp.status_code})"}, ensure_ascii=False)
                        yield f"event: error\ndata: {error_data}\n\n"
                        return
                    idx = 0
                    buffer = ""
                    async for chunk in resp.aiter_text():
                        buffer += chunk
                        # SSE 이벤트 단위로 분리 (\n\n 구분자)
                        while "\n\n" in buffer:
                            event_str, buffer = buffer.split("\n\n", 1)
                            lines = event_str.strip().splitlines()

                            # event: done / event: error 처리
                            event_type = "message"
                            data_line  = ""
                            event_id   = None
                            for line in lines:
                                if line.startswith("event:"):
                                    event_type = line[6:].strip()
                                elif line.startswith("data:"):
                                    data_line = line[5:].strip()
                                elif line.startswith("id:"):
                                    event_id = line[3:].strip()

                            if event_type == "done":
                                yield "event: done\ndata: {}\n\n"
                                return

                            if event_type == "error":
                                yield f"event: error\ndata: {data_line}\n\n"
                                return

                            if data_line:
                                # 질문 DB 저장
                                try:
                                    q_data = json.loads(data_line)
                                    q_type_str = q_data.get("question_type", "yes_no")
                                    try:
                                        q_type = AIQuestionType(q_type_str)
                                    except ValueError:
                                        q_type = AIQuestionType.yes_no

                                    ai_q = AIQuestion(
                                        daily_record_id=saved_record_id,
                                        patient_id=saved_patient_id,
                                        question_text=q_data["question_text"],
                                        reason=q_data.get("reason"),
                                        question_type=q_type,
                                        options=(
                                            json.dumps(q_data["options"], ensure_ascii=False)
                                            if q_data.get("options") is not None else None
                                        ),
                                    )
                                    db_proxy.add(ai_q)
                                    db_proxy.commit()
                                    db_proxy.refresh(ai_q)

                                    # question_id를 포함해서 클라이언트에 전송
                                    out = json.dumps({
                                        "question_id":   ai_q.id,
                                        "question_text": q_data["question_text"],
                                        "question_type": q_type_str,
                                        "options":       q_data.get("options"),
                                        "reason":        q_data.get("reason"),
                                    }, ensure_ascii=False)
                                    yield f"id: {idx}\ndata: {out}\n\n"
                                    idx += 1
                                except Exception as save_e:
                                    logger.warning(f"AI 질문 DB 저장 실패: {save_e}")
                                    # 저장 실패해도 이벤트는 그대로 전달
                                    yield f"id: {idx}\ndata: {data_line}\n\n"
                                    idx += 1

        except Exception as e:
            logger.error(f"AI SSE 프록시 실패 (record_id={saved_record_id}): {e}")
            error_data = json.dumps({"message": str(e)}, ensure_ascii=False)
            yield f"event: error\ndata: {error_data}\n\n"
        finally:
            db_proxy.close()

    return StreamingResponse(
        proxy_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
        },
    )


# ── AI 질문 답변 저장 + 요약 트리거 ─────────────────────────────────
@router.post(
    "/{record_id}/ai",
    summary="AI 질문 답변 저장 + AI 요약 백그라운드 트리거 (SSE 흐름 Step 3)",
)
def submit_ai_survey(
    record_id: int,
    body: SurveySubmitRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    AI 질문 답변을 저장하고 백그라운드에서 AI 요약을 생성한다.
    중복 제출 방지: 이미 risk_level이 있거나 요약 생성 중이면 409.
    """
    if current_user.role != UserRole.patient:
        raise HTTPException(status_code=403, detail="환자만 접근할 수 있습니다.")

    effective_record_id = body.record_id if body.record_id else record_id
    record = db.query(DailyRecord).filter(DailyRecord.id == effective_record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    if record.patient_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

    # 중복 제출 방지
    if record.risk_level is not None:
        raise HTTPException(status_code=409, detail="이미 제출된 설문입니다.")
    if effective_record_id in _summary_in_progress:
        raise HTTPException(status_code=409, detail="AI 요약이 이미 생성 중입니다.")

    # AI 질문 답변 저장
    saved = 0
    for item in body.responses:
        if item.choice is None and not item.text_answer:
            continue
        existing = (
            db.query(SurveyResponse)
            .filter(
                SurveyResponse.daily_record_id == effective_record_id,
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
                daily_record_id=effective_record_id,
                patient_id=current_user.id,
                question_id=item.question_id,
                question_type=item.question_type,
                choice=SurveyChoice(item.choice) if item.choice else None,
                text_answer=item.text_answer or "",
            ))
        saved += 1
    db.commit()

    # 요약용 데이터 수집
    exchanges = (
        db.query(ExchangeRecord)
        .filter(ExchangeRecord.daily_record_id == effective_record_id)
        .order_by(ExchangeRecord.session_number)
        .all()
    )
    exchange_list = [
        {
            "session_number":         ex.session_number,
            "exchange_time":          ex.exchange_time,
            "drainage_volume":        float(ex.drainage_volume) if ex.drainage_volume is not None else None,
            "infusion_concentration": float(ex.infusion_concentration) if ex.infusion_concentration is not None else None,
            "infusion_weight":        float(ex.infusion_weight) if ex.infusion_weight is not None else None,
            "ultrafiltration":        float(ex.ultrafiltration) if ex.ultrafiltration is not None else None,
        }
        for ex in exchanges
    ]
    record_data = {
        "date":                  str(record.record_date),
        "weight":                float(record.weight) if record.weight else None,
        "blood_pressure":        record.blood_pressure,
        "total_ultrafiltration": float(record.total_ultrafiltration) if record.total_ultrafiltration else None,
        "turbid_peritoneal":     record.turbid_peritoneal,
        "fasting_blood_glucose": float(record.fasting_blood_glucose) if record.fasting_blood_glucose else None,
        "urine_count":           record.urine_count,
        "memo":                  record.memo,
        "exchange_records":      exchange_list,
    }

    from sqlalchemy import or_
    _assigned_ids = (
        db.query(QuestionPatientAssignment.question_id)
        .filter(QuestionPatientAssignment.patient_id == current_user.id)
        .subquery()
    )
    common_qs = (
        db.query(CommonQuestion)
        .filter(
            CommonQuestion.is_active == True,
            or_(
                CommonQuestion.target_all_patients == True,
                CommonQuestion.id.in_(_assigned_ids),
            ),
        )
        .all()
    )
    responses_all = (
        db.query(SurveyResponse)
        .filter(SurveyResponse.daily_record_id == effective_record_id)
        .all()
    )
    resp_map = {(r.question_id, r.question_type): r for r in responses_all}

    common_qa = []
    for q in common_qs:
        r = resp_map.get((q.id, "common"))
        common_qa.append({
            "question_text": q.question_text,
            "choice":        r.choice.value if r and r.choice else None,
            "text_answer":   r.text_answer if r else None,
        })

    ai_qs = (
        db.query(AIQuestion)
        .filter(
            AIQuestion.daily_record_id == effective_record_id,
            AIQuestion.status != AIQuestionStatus.rejected_global,
        )
        .all()
    )
    ai_survey_responses = []
    for q in ai_qs:
        r = resp_map.get((q.id, "ai"))
        if not r:
            continue
        answer = r.text_answer or (r.choice.value if r.choice else None) or "미응답"
        ai_survey_responses.append({
            "question_text": q.question_text,
            "question_type": q.question_type.value if q.question_type else "yes_no",
            "answer":        answer,
        })

    hist_result        = compute_historical_context(db, current_user.id, effective_record_id)
    historical_context = hist_result["context"]
    historical_records = hist_result["historical_records"]

    patient_user   = db.query(User).filter(User.id == current_user.id).first()
    doctor_note_row = (
        db.query(PatientNote)
        .filter(PatientNote.patient_id == current_user.id)
        .order_by(PatientNote.updated_at.desc())
        .first()
    )
    patient_profile = {
        "self_memo":   patient_user.self_memo if patient_user and patient_user.self_memo else None,
        "doctor_note": doctor_note_row.content if doctor_note_row else None,
    }

    _summary_in_progress.add(effective_record_id)
    background_tasks.add_task(
        summary_background,
        record_id=effective_record_id,
        record_data=record_data,
        common_qa=common_qa,
        ai_survey_responses=ai_survey_responses,
        historical_context=historical_context,
        patient_profile=patient_profile,
        historical_records=historical_records,
    )

    logger.info(f"AI 질문 답변 {saved}개 저장, 요약 백그라운드 트리거 (record_id={effective_record_id})")
    return {"success": True, "saved_count": saved}
