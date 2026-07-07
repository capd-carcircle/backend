from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel


class DashboardRecordRow(BaseModel):
    """대시보드 테이블에 표시되는 기록 행"""
    record_id:            int
    patient_id:           int
    patient_name:         str
    patient_birth_date:   Optional[str] = None   # 나이 계산용
    patient_gender:       Optional[str] = None   # 'm' | 'f'
    submitted_at:         Optional[str]           # ISO 8601 문자열
    status:               str                     # submitted | reviewed | rejected
    unreviewed_ai_count:  int
    risk_level:           Optional[str]           # normal | caution | urgent | None(미완료)
    ai_summary:           Optional[str]           # AI 요약 (없으면 None)
    has_anomaly:          Optional[bool] = None    # "오늘" 조회일 때만 채워짐(현재상태 개념), 계산 이력 없으면 None
    anomaly_record_date:  Optional[str] = None     # has_anomaly 판정 기준이 된 실제 기록 날짜(오늘과 다르면 "이전부터 지속" 의미)


class PatientSummary(BaseModel):
    """환자 필터 드롭다운용 요약"""
    id:           int
    name:         str
    phone_number: str
    birth_date:   Optional[str] = None
    gender:       Optional[str] = None


class DashboardResponse(BaseModel):
    """GET /api/v1/dashboard 응답"""
    today:           str   # YYYY-MM-DD (조회 기준일)
    total_submitted: int
    pending_count:   int
    approved_count:  int
    total_patients:  int
    records:         List[DashboardRecordRow]
    patients:        List[PatientSummary]   # 환자 필터 드롭다운용
