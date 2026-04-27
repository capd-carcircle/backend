from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.crud.daily_record import (
    create_daily_record,
    delete_daily_record,
    get_patient_records,
    get_record_by_id,
    update_daily_record,
)
from app.models.question import AIQuestion, AIQuestionStatus, CommonQuestion
from app.models.record import DailyRecord, ExchangeRecord, RecordStatus
from app.models.survey import SurveyResponse
from app.models.user import User, UserRole
from app.schemas.record import DailyRecordCreate, DailyRecordResponse, DailyRecordUpdate

router = APIRouter(prefix="/records", tags=["기록"])


def _require_patient(current_user: User):
    if current_user.role != UserRole.patient:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="환자만 접근할 수 있습니다.")


def _require_doctor(current_user: User):
    if current_user.role != UserRole.doctor:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="의사만 접근할 수 있습니다.")


# ── 환자: 기록 제출 ────────────────────────────────────────
@router.post(
    "",
    response_model=DailyRecordResponse,
    status_code=status.HTTP_201_CREATED,
    summary="일일 기록 제출",
)
def submit_record(
    payload: DailyRecordCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_patient(current_user)
    record = create_daily_record(db, patient_id=current_user.id, data=payload)
    return record


# ── 환자: 내 기록 목록 ─────────────────────────────────────
@router.get(
    "",
    response_model=list[DailyRecordResponse],
    summary="내 기록 목록",
)
def get_my_records(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_patient(current_user)
    return get_patient_records(db, patient_id=current_user.id)


# ── 환자: 단건 조회 ────────────────────────────────────────
@router.get(
    "/{record_id}",
    response_model=DailyRecordResponse,
    summary="기록 단건 조회 (환자용)",
)
def get_record(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_patient(current_user)
    record = get_record_by_id(db, record_id=record_id)
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    if record.patient_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")
    return record


# ── 환자: 기록 수정 (draft 상태만 가능) ───────────────────
@router.patch(
    "/{record_id}",
    response_model=DailyRecordResponse,
    summary="일일 기록 수정 (draft 상태만)",
)
def update_record(
    record_id: int,
    payload: DailyRecordUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_patient(current_user)
    record = get_record_by_id(db, record_id=record_id)
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    if record.patient_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")
    if record.status != RecordStatus.draft:
        raise HTTPException(status_code=409, detail="최종 제출된 기록은 수정할 수 없습니다.")

    updated = update_daily_record(db, record=record, data=payload)

    return updated


# ── 환자: 기록 최종 제출 (draft → submitted + AI 생성) ─────
@router.post(
    "/{record_id}/submit",
    response_model=DailyRecordResponse,
    summary="임시저장 기록 최종 제출",
)
def finalize_record(
    record_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_patient(current_user)
    record = get_record_by_id(db, record_id=record_id)
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    if record.patient_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")
    if record.status != RecordStatus.draft:
        raise HTTPException(status_code=409, detail="이미 제출된 기록입니다.")

    # draft → submitted
    record.status = RecordStatus.submitted
    record.submitted_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(record)

    # AI 질문 생성 — Gemini가 전담 (백그라운드)
    from app.api.v1.routes.surveys import (
        _ai_question_background,
        _ai_in_progress,
        _compute_historical_context,
    )
    from app.models.survey import RejectedQPattern

    # 기존 AI 질문 초기화
    db.query(AIQuestion).filter(
        AIQuestion.daily_record_id == record_id
    ).delete()
    db.commit()

    # 오늘 기록 dict 구성
    record_data = {
        "weight":                float(record.weight) if record.weight else None,
        "blood_pressure":        record.blood_pressure,
        "total_ultrafiltration": float(record.total_ultrafiltration) if record.total_ultrafiltration else None,
        "turbid_peritoneal":     record.turbid_peritoneal,
        "fasting_blood_glucose": float(record.fasting_blood_glucose) if record.fasting_blood_glucose else None,
        "urine_count":           record.urine_count,
        "memo":                  record.memo,
    }

    # 거절된 질문 패턴 조회
    rejected_keys = [
        r.pattern for r in db.query(RejectedQPattern).filter(
            (RejectedQPattern.patient_id == current_user.id)
            | (RejectedQPattern.patient_id.is_(None))
        ).all()
    ]

    # 과거 추세 데이터 계산 (기록 1개부터 활용)
    historical_context = _compute_historical_context(db, current_user.id, record_id)

    _ai_in_progress.add(record_id)
    background_tasks.add_task(
        _ai_question_background,
        record_id=record_id,
        patient_id=current_user.id,
        record_data=record_data,
        rejected_keys=rejected_keys,
        historical_context=historical_context,
    )

    db.refresh(record)
    return record


# ── 환자: 기록 삭제 ────────────────────────────────────────
@router.delete(
    "/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="일일 기록 삭제 (submitted 상태만)",
)
def delete_record(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_patient(current_user)
    record = get_record_by_id(db, record_id=record_id)
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    if record.patient_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")
    if record.status not in (RecordStatus.draft, RecordStatus.submitted):
        raise HTTPException(status_code=409, detail="검토가 완료된 기록은 삭제할 수 없습니다.")
    delete_daily_record(db, record=record)


# ── 의사: 기록 상세 조회 ───────────────────────────────────
@router.get(
    "/{record_id}/detail",
    summary="기록 상세 조회 (의사용) — CAPD + 설문 + AI요약 + EMR",
)
def get_record_detail(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_doctor(current_user)

    record = db.query(DailyRecord).filter(DailyRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")

    patient = db.query(User).filter(User.id == record.patient_id).first()

    # ── 교환 기록 ──────────────────────────────────────────
    exchanges = (
        db.query(ExchangeRecord)
        .filter(ExchangeRecord.daily_record_id == record_id)
        .order_by(ExchangeRecord.session_number)
        .all()
    )
    exchanges_out = [
        {
            "session_number":        e.session_number,
            "exchange_time":         e.exchange_time,
            "drainage_volume":       float(e.drainage_volume) if e.drainage_volume is not None else None,
            "infusion_concentration": float(e.infusion_concentration) if e.infusion_concentration is not None else None,
            "infusion_weight":       float(e.infusion_weight) if e.infusion_weight is not None else None,
            "ultrafiltration":       float(e.ultrafiltration) if e.ultrafiltration is not None else None,
        }
        for e in exchanges
    ]

    # ── 설문: 전체 질문 + 답변 (미답변 포함) ─────────────────
    responses = (
        db.query(SurveyResponse)
        .filter(SurveyResponse.daily_record_id == record_id)
        .all()
    )
    resp_map = {(r.question_id, r.question_type): r for r in responses}

    survey_out = []

    # 공통 질문 (활성화된 것)
    for q in db.query(CommonQuestion).filter(CommonQuestion.is_active == True).all():
        r = resp_map.get((q.id, "common"))
        # text_answer가 있고 choice가 없는 경우도 answered=True로 처리
        answered = r is not None and (r.choice is not None or bool(r.text_answer))
        survey_out.append({
            "question_type":      "common",
            "question_item_type": q.question_type.value if q.question_type else "yes_no",
            "question_text":      q.question_text,
            "reason":             None,
            "choice":             r.choice.value if r and r.choice else None,
            "text_answer":        r.text_answer if r else None,
            "answered":           answered,
        })

    # AI 질문 (이 기록용)
    for q in db.query(AIQuestion).filter(
        AIQuestion.daily_record_id == record_id,
        AIQuestion.status != "rejected_global",
    ).all():
        r = resp_map.get((q.id, "ai"))
        answered = r is not None and (r.choice is not None or bool(r.text_answer))
        survey_out.append({
            "question_type":      "ai",
            "question_item_type": q.question_type.value if q.question_type else "yes_no",
            "question_text":      q.question_text,
            "reason":             q.reason,
            "choice":             r.choice.value if r and r.choice else None,
            "text_answer":        r.text_answer if r else None,
            "answered":           answered,
        })

    # ── AI 요약 (Gemini 우선, 없으면 규칙 기반 폴백) ──────────
    if record.ai_summary:
        ai_summary = record.ai_summary
    else:
        ai_summary = _build_ai_summary(record, exchanges)

    # ── EMR 형식 (Gemini emr_soap 우선, 없으면 규칙 기반 폴백) ─
    if record.emr_soap:
        emr = _parse_emr_soap(record.emr_soap)
    else:
        emr = _build_emr(record, exchanges, patient)

    return {
        "record_id":              record.id,
        "patient_id":             record.patient_id,
        "patient_name":           patient.name if patient else "알 수 없음",
        "record_date":            str(record.record_date),
        "submitted_at":           record.submitted_at.isoformat() if record.submitted_at else record.created_at.isoformat(),
        "status":                 record.status.value,
        "turbid_peritoneal":      record.turbid_peritoneal,
        "weight":                 float(record.weight) if record.weight is not None else None,
        "blood_pressure":         record.blood_pressure,
        "urine_count":            record.urine_count,
        "total_ultrafiltration":  float(record.total_ultrafiltration) if record.total_ultrafiltration is not None else None,
        "fasting_blood_glucose":  float(record.fasting_blood_glucose) if record.fasting_blood_glucose is not None else None,
        "memo":                   record.memo,
        "exchange_records":       exchanges_out,
        "survey_responses":       survey_out,
        "ai_summary":             ai_summary,
        "emr":                    emr,
    }


# ── 의사: 기록 승인 ────────────────────────────────────────
@router.patch(
    "/{record_id}/approve",
    summary="기록 승인 (의사용)",
)
def approve_record(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_doctor(current_user)

    record = db.query(DailyRecord).filter(DailyRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    if record.status == RecordStatus.reviewed:
        raise HTTPException(status_code=409, detail="이미 승인된 기록입니다.")

    record.status      = RecordStatus.reviewed
    record.approved_by = current_user.id
    record.updated_at  = datetime.now(timezone.utc)
    db.commit()
    db.refresh(record)

    return {"success": True, "message": "기록이 승인되었습니다.", "record_id": record_id}


# ── 의사: 승인 취소 (reviewed → submitted) ─────────────────
@router.patch(
    "/{record_id}/revert",
    summary="승인 취소 — 검토 중으로 되돌리기 (의사용)",
)
def revert_record(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_doctor(current_user)

    record = db.query(DailyRecord).filter(DailyRecord.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    if record.status != RecordStatus.reviewed:
        raise HTTPException(status_code=409, detail="승인된 기록이 아닙니다.")

    record.status      = RecordStatus.submitted
    record.approved_by = None
    record.updated_at  = datetime.now(timezone.utc)
    db.commit()
    db.refresh(record)

    return {"success": True, "message": "검토 중으로 되돌렸습니다.", "record_id": record_id}


# ── EMR SOAP 파서 (Gemini 결과 → dict) ────────────────────
def _parse_emr_soap(emr_soap: str) -> dict:
    """
    Gemini가 생성한 "S: ...\nO: ...\nA: ...\nP: ..." 문자열을 파싱.
    각 섹션이 없으면 빈 문자열 반환.
    """
    import re
    result = {"S": "", "O": "", "A": "", "P": ""}
    for key in ["S", "O", "A", "P"]:
        m = re.search(rf'(?:^|\n){key}:\s*(.*?)(?=\n[SOAP]:|$)', emr_soap, re.DOTALL)
        if m:
            result[key] = m.group(1).strip()
    return result


# ── AI 요약 빌더 (규칙 기반) ───────────────────────────────
def _build_ai_summary(record: DailyRecord, exchanges: list) -> str:
    parts = []

    # 총 제수량
    uf = float(record.total_ultrafiltration) if record.total_ultrafiltration is not None else None
    if uf is not None:
        sign = "+" if uf > 0 else ""
        note = " (평소 대비 낮음)" if uf < 0 else " (정상 범위)"
        parts.append(f"총 제수량 {sign}{uf:.0f}g{note}.")

    # 혈압
    if record.blood_pressure:
        try:
            systolic = int(record.blood_pressure.split("/")[0])
            if systolic > 140:
                parts.append(f"혈압 {record.blood_pressure} mmHg — KDIGO 기준 초과, 추가 모니터링 권장.")
            else:
                parts.append(f"혈압 {record.blood_pressure} mmHg — 정상 범위.")
        except Exception:
            parts.append(f"혈압 {record.blood_pressure} mmHg.")

    # 혼탁 투석액
    if record.turbid_peritoneal:
        parts.append("혼탁 투석액 관찰 — 복막염 의심, 즉시 확인 필요.")
    else:
        parts.append("흐린 투석액 미관찰.")

    # 체중
    if record.weight is not None:
        parts.append(f"체중 {float(record.weight):.1f} kg 기록.")

    # 혈당
    if record.fasting_blood_glucose is not None:
        bg = float(record.fasting_blood_glucose)
        if bg > 180:
            parts.append(f"공복 혈당 {bg:.0f} mg/dL — 이상 수치, 식이 관리 확인 권장.")
        else:
            parts.append(f"공복 혈당 {bg:.0f} mg/dL.")

    # 투석액 농도 (2.5% 비중)
    high_conc = [e for e in exchanges if e.infusion_concentration and float(e.infusion_concentration) >= 2.5]
    if high_conc:
        parts.append(f"2.5% 고농도 투석액 {len(high_conc)}회 사용.")

    return " ".join(parts) if parts else "기록이 정상 범위 내에 있습니다."


# ── EMR 빌더 (S/O/A/P) ────────────────────────────────────
def _build_emr(record: DailyRecord, exchanges: list, patient: User) -> dict:
    total_drain = sum(
        float(e.drainage_volume) for e in exchanges if e.drainage_volume is not None
    )
    uf   = float(record.total_ultrafiltration) if record.total_ultrafiltration is not None else None
    bp   = record.blood_pressure or "미측정"
    wt   = f"{float(record.weight):.1f} kg" if record.weight is not None else "미측정"
    bg   = f"{float(record.fasting_blood_glucose):.0f} mg/dL" if record.fasting_blood_glucose is not None else "미측정"
    sess = len([e for e in exchanges if e.exchange_time])

    # S
    s_problems = []
    if record.turbid_peritoneal:
        s_problems.append("혼탁 투석액 관찰")
    memo_str = f" 메모: {record.memo}" if record.memo else ""
    s = f"환자 CAPD {sess}회 시행.{' ' + ', '.join(s_problems) + '.' if s_problems else ' 복통 없음. 흐린 투석액 없음.'}{memo_str}"

    # O
    uf_str = f"{uf:+.0f} g" if uf is not None else "미기록"
    o = f"체중 {wt}, 혈압 {bp} mmHg, 공복혈당 {bg} / 총 제수량 {uf_str} / 총 배액량 {total_drain:.0f} g"

    # A
    a_items = []
    if uf is not None and uf < 0:
        a_items.append("제수량 부족 가능성")
    try:
        if record.blood_pressure and int(record.blood_pressure.split("/")[0]) > 140:
            a_items.append("혈압 상승 주의 필요")
    except Exception:
        pass
    if record.turbid_peritoneal:
        a_items.append("복막염 의심")
    a = ". ".join(a_items) + "." if a_items else "특이 소견 없음."

    # P
    p_items = []
    try:
        if record.blood_pressure and int(record.blood_pressure.split("/")[0]) > 140:
            p_items.append("혈압 모니터링 강화")
    except Exception:
        pass
    if uf is not None and uf < 0:
        p_items.append("수분 섭취 제한 교육 권고")
    if record.turbid_peritoneal:
        p_items.append("투석액 배양 검사 시행")
    p = ". ".join(p_items) + "." if p_items else "현 치료 계획 유지."

    return {"S": s, "O": o, "A": a, "P": p}
