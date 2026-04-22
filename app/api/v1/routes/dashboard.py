from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.question import AIQuestion, AIQuestionStatus
from app.models.record import DailyRecord, RecordStatus
from app.models.user import User, UserRole
from app.schemas.dashboard import DashboardRecordRow, DashboardResponse, PatientSummary

router = APIRouter(prefix="/dashboard", tags=["대시보드"])


def _require_doctor(current_user: User) -> None:
	if current_user.role != UserRole.doctor:
		raise HTTPException(
			status_code=status.HTTP_403_FORBIDDEN,
			detail="의사만 접근할 수 있습니다.",
		)


@router.get(
	"",
	response_model=DashboardResponse,
	summary="의사 대시보드",
	description="지정 날짜(기본: 오늘)에 제출된 환자 기록 목록과 통계를 반환합니다. patient_id로 특정 환자 필터링 가능.",
)
def get_dashboard(
	db: Session = Depends(get_db),
	current_user: User = Depends(get_current_user),
	record_date: Optional[date] = Query(
		default=None,
		description="조회 기준일 (YYYY-MM-DD). 미입력 시 오늘.",
	),
	patient_id: Optional[int] = Query(
		default=None,
		description="특정 환자 ID. 미입력 시 전체 환자.",
	),
) -> DashboardResponse:
	_require_doctor(current_user)

	target_date = record_date or date.today()

	# ── 활성 환자 목록 (드롭다운용) ───────────────────────────
	all_patients: List[User] = (
		db.query(User)
		.filter(User.role == UserRole.patient, User.is_active == True)
		.order_by(User.name)
		.all()
	)
	total_patients = len(all_patients)
	patients_out = [PatientSummary(id=p.id, name=p.name) for p in all_patients]

	# ── 해당 날짜 기록 목록 (환자 정보 JOIN) ──────────────────
	query = (
		db.query(DailyRecord, User)
		.join(User, DailyRecord.patient_id == User.id)
		.filter(DailyRecord.record_date == target_date)
	)
	if patient_id is not None:
		query = query.filter(DailyRecord.patient_id == patient_id)

	day_records: List[tuple] = query.order_by(DailyRecord.submitted_at.desc()).all()

	# ── 미검토 AI 질문 수 — record_id별로 한 번에 집계 ─────────
	record_ids = [rec.id for rec, _ in day_records]
	ai_counts: dict[int, int] = {}
	if record_ids:
		rows = (
			db.query(
				AIQuestion.daily_record_id,
				func.count(AIQuestion.id).label("cnt"),
			)
			.filter(
				AIQuestion.daily_record_id.in_(record_ids),
				AIQuestion.status == AIQuestionStatus.pending,
			)
			.group_by(AIQuestion.daily_record_id)
			.all()
		)
		ai_counts = {row.daily_record_id: row.cnt for row in rows}

	# ── 통계 계산 ─────────────────────────────────────────────
	total_submitted = len(day_records)
	pending_count   = sum(1 for rec, _ in day_records if rec.status == RecordStatus.submitted)
	approved_count  = sum(1 for rec, _ in day_records if rec.status == RecordStatus.reviewed)

	# ── 기록 행 조립 ──────────────────────────────────────────
	records_out = [
		DashboardRecordRow(
			record_id           = rec.id,
			patient_id          = rec.patient_id,
			patient_name        = patient.name,
			submitted_at        = rec.submitted_at.isoformat() if rec.submitted_at else None,
			status              = rec.status.value,
			unreviewed_ai_count = ai_counts.get(rec.id, 0),
		)
		for rec, patient in day_records
	]

	return DashboardResponse(
		today           = target_date.isoformat(),
		total_submitted = total_submitted,
		pending_count   = pending_count,
		approved_count  = approved_count,
		total_patients  = total_patients,
		records         = records_out,
		patients        = patients_out,
	)
