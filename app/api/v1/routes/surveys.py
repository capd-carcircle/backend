"""
설문 API
- 공통 질문 + AI 맞춤 질문 조회/응답
- AI 질문 생성: Gemini가 KDIGO 기반으로 전담, ai/ 서버(포트 8001) 백그라운드 호출
- 과거 추세(historical_context) 반영하여 트렌드 기반 질문 생성
- 설문 완료 시 AI 종합 요약 트리거
"""
import json
import logging
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.patient_assignment import PatientDoctorAssignment
from app.models.question import AIQuestion, AIQuestionStatus, CommonQuestion, QuestionPatientAssignment
from app.models.patient_note import PatientNote
from app.models.record import DailyRecord
from app.models.survey import SurveyResponse, SurveyChoice
from app.models.user import User, UserRole
from app.schemas.question import AIQuestionResponse
from app.schemas.survey import SurveySubmitRequest, SurveySubmitResponse
from app.services.ai_background import (
    _ai_in_progress,
    _summary_in_progress,
    ai_question_background,
    summary_background,
    compute_historical_context,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/surveys", tags=["설문"])

MAX_AI_QUESTIONS = 5   # Gemini가 생성하는 AI 질문 최대 개수


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
    ai_pending        = record_id in _ai_in_progress
    survey_completed  = record.risk_level is not None or record_id in _summary_in_progress

    return {
        "common_questions": common_out,
        "ai_questions":     ai_out,
        "total_count":      total_count,
        "answered_count":   answered_count,
        "ai_pending":       ai_pending,
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

    # 응답 데이터 수집 (백그라운드 함수에 넘길 snapshot)
    record_data = {
        "blood_pressure":        record.blood_pressure,
        "weight":                float(record.weight) if record.weight else None,
        "total_ultrafiltration": float(record.total_ultrafiltration) if record.total_ultrafiltration else None,
        "fasting_blood_glucose": float(record.fasting_blood_glucose) if record.fasting_blood_glucose else None,
        "turbid_peritoneal":     record.turbid_peritoneal,
        "urine_count":           record.urine_count,
        "memo":                  record.memo,
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

    historical_context = compute_historical_context(db, current_user.id, record_id)

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
    )

    logger.info(f"설문 완료 — AI 요약 백그라운드 트리거 (record_id={record_id})")
    return {"success": True}
