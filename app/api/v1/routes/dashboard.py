from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.patient_assignment import PatientDoctorAssignment
from app.models.question import AIQuestion, AIQuestionStatus
from app.models.record import DailyRecord, RecordStatus
from app.models.registration import PatientRegistration, RegistrationStatus
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

	# ── target_date 당일 끝(23:59:59 UTC) ───────────────────
	target_date_end = datetime(
		target_date.year, target_date.month, target_date.day,
		23, 59, 59, tzinfo=timezone.utc
	)
	target_date_start = datetime(
		target_date.year, target_date.month, target_date.day,
		0, 0, 0, tzinfo=timezone.utc
	)

	# ── target_date 기준 담당 환자 ID 집합 ──────────────────
	# assignment 기반: started_at <= target_date AND (ended_at IS NULL OR ended_at >= target_date)
	assign_patient_ids = (
		db.query(PatientDoctorAssignment.patient_id)
		.filter(
			PatientDoctorAssignment.doctor_id == current_user.id,
			PatientDoctorAssignment.started_at <= target_date_end,
			or_(
				PatientDoctorAssignment.ended_at.is_(None),
				PatientDoctorAssignment.ended_at >= target_date_start,
			),
		)
		.subquery()
	)
	patient_filter = or_(
		User.id.in_(assign_patient_ids),
		User.doctor_id == current_user.id,  # 레거시 호환
	)

	# ── 활성 환자 목록 (target_date 당시 기준 — 가입일 필터) ──
	all_patients: List[User] = (
		db.query(User)
		.filter(
			User.role == UserRole.patient,
			User.is_active == True,
			User.created_at <= target_date_end,
			patient_filter,
		)
		.order_by(User.name)
		.all()
	)
	total_patients = len(all_patients)
	patients_out = [PatientSummary(id=p.id, name=p.name, phone_number=p.phone_number, birth_date=p.birth_date, gender=p.gender) for p in all_patients]

	# ── 해당 날짜 기록 목록 (환자 정보 JOIN) ─────────────────
	query = (
		db.query(DailyRecord, User)
		.join(User, DailyRecord.patient_id == User.id)
		.filter(
			DailyRecord.record_date == target_date,
			patient_filter,
		)
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

	# ── 이상치 캐시 조회 (Gold: patient_daily_analytics) ────────
	# 이상치는 "그 날짜의 기록이 어땠는가"가 아니라 "이 환자가 지금 이상 소견이
	# 있는가"를 나타내는 현재-상태 개념으로 정리(2026-07-08, 차원 확인 — 날짜별로
	# 다르게 보여줄 실익이 없고, 오히려 "정확히 그 날짜 캐시만 인정" 조건 때문에
	# 하루만 지나도 어제 화면에서 배지가 사라지는 혼란만 있었음). 그래서 실제
	# 달력 기준 "오늘"을 보는 중일 때만 patients/overview와 동일하게 환자별
	# "가장 최근" 캐시를 조회해서 보여주고, 과거 날짜를 캘린더로 조회할 때는
	# 애초에 조회하지 않고 항상 None(배지 없음) — 과거 기록 열람 화면에 "현재"
	# 상태를 갖다 붙이면 오히려 그 날짜에 문제가 있었다는 것처럼 오인될 수 있음.
	anomaly_by_patient: dict[int, bool] = {}
	day_patient_ids = list({rec.patient_id for rec, _ in day_records})
	if day_patient_ids and target_date == date.today():
		try:
			rows2 = db.execute(
				text("""
					SELECT DISTINCT ON (patient_id) patient_id, has_anomaly
					FROM patient_daily_analytics
					WHERE patient_id = ANY(:ids)
					ORDER BY patient_id, record_date DESC
				"""),
				{"ids": day_patient_ids},
			).fetchall()
			anomaly_by_patient = {r.patient_id: r.has_anomaly for r in rows2}
		except Exception:
			anomaly_by_patient = {}   # 캐시 조회 실패해도 대시보드는 정상 반환 (best-effort)

	# ── 통계 계산 ─────────────────────────────────────────────
	total_submitted = len(day_records)
	pending_count   = sum(1 for rec, _ in day_records if rec.status == RecordStatus.submitted)
	approved_count  = sum(1 for rec, _ in day_records if rec.status == RecordStatus.reviewed)

	# ── 기록 행 조립 ──────────────────────────────────────────
	records_out = [
		DashboardRecordRow(
			record_id            = rec.id,
			patient_id           = rec.patient_id,
			patient_name         = patient.name,
			patient_birth_date   = patient.birth_date,
			patient_gender       = patient.gender,
			submitted_at         = rec.submitted_at.isoformat() if rec.submitted_at else None,
			status               = rec.status.value,
			unreviewed_ai_count  = ai_counts.get(rec.id, 0),
			risk_level           = rec.risk_level.value if rec.risk_level else None,
			ai_summary           = rec.ai_summary,
			has_anomaly          = anomaly_by_patient.get(rec.patient_id),
		)
		for rec, patient in day_records
	]

	# 긴급 환자 최상단 고정 (urgent → caution → normal → None 순)
	_risk_order = {"urgent": 0, "caution": 1, "normal": 2, None: 3}
	records_out.sort(key=lambda r: _risk_order.get(r.risk_level, 3))

	return DashboardResponse(
		today           = target_date.isoformat(),
		total_submitted = total_submitted,
		pending_count   = pending_count,
		approved_count  = approved_count,
		total_patients  = total_patients,
		records         = records_out,
		patients        = patients_out,
	)
