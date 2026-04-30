"""
설문 API
- 공통 질문 + AI 맞춤 질문 조회/응답
- AI 질문 생성: Gemini가 KDIGO 기반으로 전담, ai/ 서버(포트 8001) 백그라운드 호출
- 과거 추세(historical_context) 반영하여 트렌드 기반 질문 생성
- 설문 완료 시 AI 종합 요약 트리거
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.core.auth import get_current_user
from app.core.database import get_db, SessionLocal
from app.models.patient_assignment import PatientDoctorAssignment
from app.models.question import AIQuestion, AIQuestionStatus, AIQuestionType, CommonQuestion, QuestionPatientAssignment
from app.models.patient_note import PatientNote
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

# 백그라운드 AI 요약 생성 중인 record_id 추적 (중복 제출 방지)
_summary_in_progress: set = set()

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


# ── AI 서버 백그라운드 질문 생성 (Gemini 전담) ───────────
def _ai_question_background(
    record_id: int,
    patient_id: int,
    record_data: dict,
    rejected_keys: list,
    historical_context: dict = None,
    patient_profile: dict = None,
):
    """
    백그라운드에서 ai/ 서버의 /ai-questions/generate 호출
    Gemini가 KDIGO 기반으로 3~5개 질문 전담 생성 → DB 저장
    historical_context로 과거 추세 반영, patient_profile로 개인화
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
        with httpx.Client(timeout=90.0) as client:
            resp = client.post(
                f"{AI_SERVER_URL}/ai-questions/generate",
                json={
                    "record_data":        record_data,
                    "rejected_keys":      rejected_keys,
                    "historical_context": historical_context or {},
                    "patient_profile":    patient_profile or {},
                },
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
                options=json.dumps(q_data["options"], ensure_ascii=False) if q_data.get("options") is not None else None,
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


# ── 과거 기록 추세 계산 ───────────────────────────────────
def _compute_historical_context(db: Session, patient_id: int, current_record_id: int) -> dict:
    """
    오늘 이전 기록을 최대 30일치 집계하여 추세 컨텍스트 반환.
    기록이 1개 이상이면 집계 결과를 반환 (트렌드 판단은 3개 이상일 때만 의미 있음).
    기록이 없으면 빈 dict 반환.
    """
    from datetime import date as date_type
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=30)

    records = (
        db.query(DailyRecord)
        .filter(
            DailyRecord.patient_id == patient_id,
            DailyRecord.id != current_record_id,
            DailyRecord.record_date >= cutoff,
        )
        .order_by(DailyRecord.record_date.desc())
        .all()
    )

    if len(records) < 1:
        return {}

    days = len(records)

    # 혈압 집계
    bp_systolics = []
    for r in records:
        try:
            bp_systolics.append(int(r.blood_pressure.split("/")[0]))
        except Exception:
            pass
    bp_ctx = {}
    if bp_systolics:
        bp_ctx = {
            "avg": str(round(sum(bp_systolics) / len(bp_systolics))),
            "max": str(max(bp_systolics)),
            "min": str(min(bp_systolics)),
            "trend": "상승" if len(bp_systolics) >= 3 and bp_systolics[0] > bp_systolics[-1] + 5
                     else "하강" if len(bp_systolics) >= 3 and bp_systolics[0] < bp_systolics[-1] - 5
                     else "안정",
        }

    # 체중 집계
    weights = [float(r.weight) for r in records if r.weight is not None]
    wt_ctx = {}
    if weights:
        avg_wt = round(sum(weights) / len(weights), 1)
        recent_7 = weights[:7]
        old_7 = weights[7:14] if len(weights) >= 14 else weights[len(recent_7):]
        delta_7d = round(recent_7[0] - recent_7[-1], 1) if len(recent_7) >= 2 else 0.0
        wt_ctx = {
            "avg": avg_wt,
            "delta_7d": delta_7d,
            "trend": "증가" if delta_7d > 0.5 else "감소" if delta_7d < -0.5 else "안정",
        }

    # UF량 주간 평균
    ufs = [(r.record_date, float(r.total_ultrafiltration)) for r in records if r.total_ultrafiltration is not None]
    uf_ctx = {}
    if ufs:
        # 주차별 평균 (최대 3주)
        weekly: list[list[float]] = [[], [], []]
        for rec_date, uf_val in ufs:
            days_ago = (datetime.now(timezone.utc).date() - rec_date).days
            week_idx = min(days_ago // 7, 2)
            weekly[week_idx].append(uf_val)
        weekly_avgs = [round(sum(w) / len(w)) for w in weekly if w]
        uf_trend = "감소" if len(weekly_avgs) >= 2 and weekly_avgs[0] < weekly_avgs[-1] - 100 \
                   else "증가" if len(weekly_avgs) >= 2 and weekly_avgs[0] > weekly_avgs[-1] + 100 \
                   else "안정"
        uf_ctx = {"weekly_avg": weekly_avgs, "trend": uf_trend}

    # 혈당 집계
    glucoses = [float(r.fasting_blood_glucose) for r in records if r.fasting_blood_glucose is not None]
    gl_ctx = {}
    if glucoses:
        gl_ctx = {
            "avg": round(sum(glucoses) / len(glucoses), 1),
            "max": max(glucoses),
        }

    # 위험도 이력
    risk_summary = {"urgent": 0, "caution": 0, "normal": 0}
    for r in records:
        if r.risk_level:
            key = r.risk_level.value if hasattr(r.risk_level, "value") else str(r.risk_level)
            if key in risk_summary:
                risk_summary[key] += 1

    return {
        "days": days,
        "bp": bp_ctx,
        "weight": wt_ctx,
        "uf": uf_ctx,
        "glucose": gl_ctx,
        "risk_summary": risk_summary,
    }


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

    historical_context = _compute_historical_context(db, current_user.id, record_id)

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
        _summary_background,
        record_id=record_id,
        record_data=record_data,
        common_qa=common_qa,
        ai_survey_responses=ai_survey_responses,
        historical_context=historical_context,
        patient_profile=patient_profile,
    )

    logger.info(f"설문 완료 — AI 요약 백그라운드 트리거 (record_id={record_id})")
    return {"success": True}


# ── AI 요약 백그라운드 함수 ───────────────────────────────
def _summary_background(
    record_id: int,
    record_data: dict,
    common_qa: list,
    ai_survey_responses: list,
    historical_context: dict,
    patient_profile: dict,
):
    """Background: generate AI summary/risk/EMR and save to DB."""
    db = SessionLocal()
    try:
        with httpx.Client(timeout=90.0) as client:
            resp = client.post(
                f"{AI_SERVER_URL}/summary",
                json={
                    "record_data":         record_data,
                    "common_qa":           common_qa,
                    "ai_survey_responses": ai_survey_responses,
                    "historical_context":  historical_context,
                    "patient_profile":     patient_profile,
                },
            )
            resp.raise_for_status()
            result = resp.json()

        record = db.query(DailyRecord).filter(DailyRecord.id == record_id).first()
        if record:
            record.risk_level = RiskLevel(result["risk_level"])
            record.ai_summary = result["ai_summary"]
            record.emr_soap   = result["emr_soap"]
            db.commit()
            logger.info(f"AI summary saved (record_id={record_id}, risk={result['risk_level']})")

    except Exception as e:
        logger.error(f"AI summary background failed (record_id={record_id}): {e}")
        db.rollback()
    finally:
        _summary_in_progress.discard(record_id)
        db.close()
