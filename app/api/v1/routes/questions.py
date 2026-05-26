import json
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.patient_assignment import PatientDoctorAssignment
from app.models.question import (
    AIQuestion, AIQuestionStatus, AIQuestionType,
    CommonQuestion, QuestionPatientAssignment,
)
from app.models.record import DailyRecord
from app.models.survey import RejectedQPattern, SurveyResponse
from app.models.user import User, UserRole
from app.schemas.question import (
    CommonQuestionCreate, CommonQuestionUpdate, CommonQuestionResponse
)

router = APIRouter(prefix="/questions", tags=["질문"])


def _require_doctor(current_user: User):
    if current_user.role != UserRole.doctor:
        raise HTTPException(status_code=403, detail="의사만 접근할 수 있습니다.")


def _verify_doctor_patient_access(db: Session, doctor_id: int, patient_id: int):
    """의사-환자 담당 관계 확인 (현재 또는 과거 담당 모두 허용)."""
    if not db.query(PatientDoctorAssignment).filter(
        PatientDoctorAssignment.doctor_id == doctor_id,
        PatientDoctorAssignment.patient_id == patient_id,
    ).first():
        raise HTTPException(status_code=403, detail="해당 환자의 담당 의사가 아닙니다.")


def _build_response(q: CommonQuestion) -> CommonQuestionResponse:
    assigned_ids = [a.patient_id for a in q.patient_assignments]
    return CommonQuestionResponse(
        id=q.id,
        question_text=q.question_text,
        question_type=q.question_type.value,
        options=q.options,
        is_active=q.is_active,
        target_all_patients=q.target_all_patients,
        assigned_patient_ids=assigned_ids,
        created_at=q.created_at,
        updated_at=q.updated_at,
    )


def _sync_assignments(db: Session, question: CommonQuestion, patient_ids: List[int]):
    db.query(QuestionPatientAssignment).filter(
        QuestionPatientAssignment.question_id == question.id
    ).delete(synchronize_session=False)
    for pid in set(patient_ids):
        db.add(QuestionPatientAssignment(question_id=question.id, patient_id=pid))


def _delete_survey_responses(db: Session, question_id: int):
    import logging
    deleted = (
        db.query(SurveyResponse)
        .filter(
            SurveyResponse.question_id == question_id,
            SurveyResponse.question_type == "common",
        )
        .delete(synchronize_session=False)
    )
    if deleted:
        logging.getLogger(__name__).info(
            f"질문 수정으로 survey_responses {deleted}건 삭제 (question_id={question_id})"
        )


# ── 목록 조회 ──────────────────────────────────────────────
@router.get(
    "/common",
    response_model=List[CommonQuestionResponse],
    summary="공통 질문 목록 (active 필터 지원)",
)
def list_common_questions(
    active: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(CommonQuestion)
    if active is not None:
        q = q.filter(CommonQuestion.is_active == active)
    questions = q.order_by(CommonQuestion.created_at.asc()).all()
    return [_build_response(q) for q in questions]


# ── 생성 ───────────────────────────────────────────────────
@router.post(
    "/common",
    response_model=CommonQuestionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="공통 질문 생성 (의사 전용)",
)
def create_common_question(
    body: CommonQuestionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_doctor(current_user)
    try:
        q_type = AIQuestionType(body.question_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"유효하지 않은 질문 유형: {body.question_type}")

    options_json = None
    if q_type in (AIQuestionType.single_select, AIQuestionType.multi_select):
        if body.options:
            options_json = json.dumps(body.options, ensure_ascii=False)

    q = CommonQuestion(
        doctor_id=current_user.id,
        question_text=body.question_text,
        question_type=q_type,
        options=options_json,
        target_all_patients=body.target_all_patients,
    )
    db.add(q)
    db.flush()

    if not body.target_all_patients and body.patient_ids:
        _sync_assignments(db, q, body.patient_ids)

    db.commit()
    db.refresh(q)
    return _build_response(q)


# ── 수정 ───────────────────────────────────────────────────
@router.patch(
    "/common/{question_id}",
    response_model=CommonQuestionResponse,
    summary="공통 질문 수정 (의사 전용)",
)
def update_common_question(
    question_id: int,
    body: CommonQuestionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_doctor(current_user)
    q = db.query(CommonQuestion).filter(CommonQuestion.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="질문을 찾을 수 없습니다.")

    content_changed = False

    if body.question_text is not None and body.question_text != q.question_text:
        q.question_text = body.question_text
        content_changed = True

    if body.question_type is not None:
        try:
            new_type = AIQuestionType(body.question_type)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"유효하지 않은 질문 유형: {body.question_type}")
        if new_type != q.question_type:
            q.question_type = new_type
            content_changed = True
            if new_type not in (AIQuestionType.single_select, AIQuestionType.multi_select):
                q.options = None

    if body.options is not None:
        cur_type = q.question_type
        if cur_type in (AIQuestionType.single_select, AIQuestionType.multi_select):
            new_opts = json.dumps(body.options, ensure_ascii=False) if body.options else None
        else:
            new_opts = None
        if new_opts != q.options:
            q.options = new_opts
            content_changed = True
    elif body.question_type is not None:
        new_type = AIQuestionType(body.question_type)
        if new_type not in (AIQuestionType.single_select, AIQuestionType.multi_select):
            if q.options is not None:
                q.options = None
                content_changed = True

    if body.is_active is not None:
        q.is_active = body.is_active

    if content_changed:
        _delete_survey_responses(db, question_id)

    if body.target_all_patients is not None:
        q.target_all_patients = body.target_all_patients
        if body.target_all_patients:
            db.query(QuestionPatientAssignment).filter(
                QuestionPatientAssignment.question_id == question_id
            ).delete(synchronize_session=False)

    if body.patient_ids is not None:
        _sync_assignments(db, q, body.patient_ids)

    q.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(q)
    return _build_response(q)


# ── 삭제 ───────────────────────────────────────────────────
@router.delete(
    "/common/{question_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="공통 질문 삭제 (의사 전용)",
)
def delete_common_question(
    question_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_doctor(current_user)
    q = db.query(CommonQuestion).filter(CommonQuestion.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="질문을 찾을 수 없습니다.")
    db.delete(q)
    db.commit()


# ── 활성/비활성 토글 ───────────────────────────────────────
@router.patch(
    "/common/{question_id}/toggle",
    response_model=CommonQuestionResponse,
    summary="공통 질문 활성/비활성 전환 (의사 전용)",
)
def toggle_common_question(
    question_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_doctor(current_user)
    q = db.query(CommonQuestion).filter(CommonQuestion.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="질문을 찾을 수 없습니다.")
    q.is_active = not q.is_active
    q.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(q)
    return _build_response(q)


# ── AI 질문 ────────────────────────────────────────────────
class AIQuestionRejectRequest(BaseModel):
    scope: str  # "patient" | "global"


@router.get(
    "/ai",
    summary="담당 환자 AI 질문 목록 조회 (의사 전용)",
)
def list_ai_questions(
    patient_id: Optional[int] = Query(None, description="특정 환자 필터링"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_doctor(current_user)

    patient_ids = [
        row[0]
        for row in db.query(PatientDoctorAssignment.patient_id).filter(
            PatientDoctorAssignment.doctor_id == current_user.id,
            PatientDoctorAssignment.ended_at.is_(None),
            PatientDoctorAssignment.started_at <= datetime.now(timezone.utc),
        ).all()
    ]

    if not patient_ids:
        return []

    query = (
        db.query(AIQuestion, DailyRecord, User)
        .join(DailyRecord, AIQuestion.daily_record_id == DailyRecord.id)
        .join(User, AIQuestion.patient_id == User.id)
        .filter(
            AIQuestion.patient_id.in_(patient_ids),
        )
    )
    if patient_id:
        query = query.filter(AIQuestion.patient_id == patient_id)

    rows = query.order_by(AIQuestion.created_at.desc()).limit(200).all()

    return [
        {
            "id":            q.id,
            "patient_id":          q.patient_id,
            "patient_name":        patient.name,
            "patient_birth_date":  patient.birth_date,
            "patient_gender":      patient.gender,
            "record_id":           record.id,
            "record_date":   record.record_date.isoformat(),
            "question_text": q.question_text,
            "question_type": q.question_type.value,
            "reason":        q.reason,
            "status":        q.status.value,
            "created_at":    q.created_at.isoformat(),
        }
        for q, record, patient in rows
    ]


@router.post(
    "/ai/{question_id}/reject",
    summary="AI 질문 거절 (의사 전용)",
)
def reject_ai_question(
    question_id: int,
    body: AIQuestionRejectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_doctor(current_user)

    if body.scope not in ("patient", "global"):
        raise HTTPException(status_code=400, detail="scope는 'patient' 또는 'global'이어야 합니다.")

    q = db.query(AIQuestion).filter(AIQuestion.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="질문을 찾을 수 없습니다.")
    _verify_doctor_patient_access(db, current_user.id, q.patient_id)

    if body.scope == "global":
        q.status = AIQuestionStatus.rejected_global
        existing = db.query(RejectedQPattern).filter(
            RejectedQPattern.pattern == q.question_text,
            RejectedQPattern.patient_id.is_(None),
        ).first()
        if not existing:
            db.add(RejectedQPattern(pattern=q.question_text, patient_id=None))
    else:
        q.status = AIQuestionStatus.rejected_for_patient
        existing = db.query(RejectedQPattern).filter(
            RejectedQPattern.pattern == q.question_text,
            RejectedQPattern.patient_id == q.patient_id,
        ).first()
        if not existing:
            db.add(RejectedQPattern(pattern=q.question_text, patient_id=q.patient_id))

    db.commit()
    return {"success": True, "scope": body.scope, "question_id": question_id}


@router.patch(
    "/ai/{question_id}/review",
    summary="AI 질문 확인 토글 (pending ↔ approved, 의사 전용)",
)
def review_ai_question(
    question_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_doctor(current_user)

    q = db.query(AIQuestion).filter(AIQuestion.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="질문을 찾을 수 없습니다.")
    _verify_doctor_patient_access(db, current_user.id, q.patient_id)

    if q.status == AIQuestionStatus.pending:
        q.status = AIQuestionStatus.approved
    elif q.status == AIQuestionStatus.approved:
        q.status = AIQuestionStatus.pending
    else:
        raise HTTPException(
            status_code=400,
            detail="거절된 질문은 복구 엔드포인트(/restore)를 사용하세요.",
        )

    db.commit()
    return {"success": True, "status": q.status.value, "question_id": question_id}


@router.post(
    "/ai/{question_id}/restore",
    summary="AI 질문 복구 (거절 → 검토 대기, 의사 전용)",
)
def restore_ai_question(
    question_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_doctor(current_user)

    q = db.query(AIQuestion).filter(AIQuestion.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="질문을 찾을 수 없습니다.")
    _verify_doctor_patient_access(db, current_user.id, q.patient_id)
    if q.status not in (AIQuestionStatus.rejected_for_patient, AIQuestionStatus.rejected_global):
        raise HTTPException(status_code=400, detail="거절된 질문만 복구할 수 있습니다.")

    if q.status == AIQuestionStatus.rejected_global:
        db.query(RejectedQPattern).filter(
            RejectedQPattern.pattern == q.question_text,
            RejectedQPattern.patient_id.is_(None),
        ).delete(synchronize_session=False)
    else:
        db.query(RejectedQPattern).filter(
            RejectedQPattern.pattern == q.question_text,
            RejectedQPattern.patient_id == q.patient_id,
        ).delete(synchronize_session=False)

    q.status = AIQuestionStatus.pending
    db.commit()
    return {"success": True, "question_id": question_id}
