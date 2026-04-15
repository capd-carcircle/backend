from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.crud.daily_record import create_daily_record, get_patient_records, get_record_by_id
from app.models.user import User, UserRole
from app.schemas.record import DailyRecordCreate, DailyRecordResponse

router = APIRouter(prefix="/records", tags=["기록"])


def _require_patient(current_user: User):
    if current_user.role != UserRole.patient:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="환자만 접근할 수 있습니다.",
        )


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
    """환자가 오늘의 CAPD 기록을 제출합니다."""
    _require_patient(current_user)
    record = create_daily_record(db, patient_id=current_user.id, data=payload)
    return record


@router.get(
    "",
    response_model=list[DailyRecordResponse],
    summary="내 기록 목록",
)
def get_my_records(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """환자 본인의 기록 목록을 최신순으로 반환합니다."""
    _require_patient(current_user)
    return get_patient_records(db, patient_id=current_user.id)


@router.get(
    "/{record_id}",
    response_model=DailyRecordResponse,
    summary="기록 단건 조회",
)
def get_record(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """특정 기록을 조회합니다. 본인 기록만 접근 가능."""
    _require_patient(current_user)
    record = get_record_by_id(db, record_id=record_id)
    if not record:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    if record.patient_id != current_user.id:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")
    return record
