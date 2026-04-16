from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.record import DailyRecord, RecordStatus
from app.models.question import AIQuestion, AIQuestionStatus
from app.models.user import User, UserRole

router = APIRouter(prefix="/dashboard", tags=["대시보드"])


def _require_doctor(current_user: User):
	if current_user.role != UserRole.doctor:
		raise HTTPException(
			status_code=status.HTTP_403_FORBIDDEN,
			detail="의사만 접근할 수 있습니다.",
		)


@router.get("", summary="의사 대시보드")
def get_dashboard(
	db: Session = Depends(get_db),
	current_user: User = Depends(get_current_user),
):
	_require_doctor(current_user)

	today = date.today()

	# 🔥 핵심 수정: record_date → date
	today_records = (
		db.query(DailyRecord)
		.filter(DailyRecord.record_date == today)
		.order_by(DailyRecord.submitted_at.desc())
		.all()
	)

	total_patients = (
		db.query(User)
		.filter(User.role == UserRole.patient, User.is_active == True)
		.count()
	)

	total_submitted = len(today_records)
	pending_count = sum(1 for r in today_records if r.status == RecordStatus.submitted)
	approved_count = sum(1 for r in today_records if r.status == RecordStatus.reviewed)

	records_out = []
	for rec in today_records:
		patient = db.query(User).filter(User.id == rec.patient_id).first()

		# 🔥 핵심 수정: status → ai_status
		unreviewed_ai_count = (
			db.query(AIQuestion)
			.filter(
				AIQuestion.daily_record_id == rec.id,
				AIQuestion.ai_status == AIQuestionStatus.pending,
			)
			.count()
		)

		records_out.append({
			"record_id": rec.id,
			"patient_id": rec.patient_id,
			"patient_name": patient.name if patient else "알 수 없음",
			"submitted_at": rec.submitted_at.isoformat() if rec.submitted_at else None,
			"status": rec.status,
			"unreviewed_ai_count": unreviewed_ai_count,
		})

	return {
		"today": today.isoformat(),
		"total_submitted": total_submitted,
		"pending_count": pending_count,
		"approved_count": approved_count,
		"total_patients": total_patients,
		"records": records_out,
	}