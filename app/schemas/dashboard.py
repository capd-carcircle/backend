from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel


class DashboardRecordRow(BaseModel):
    """대시보드 테이블에 표시되는 기록 행"""
    record_id:           int
    patient_id:          int
    patient_name:        str
    submitted_at:        Optional[str]  # ISO 8601 문자열
    status:              str            # submitted | reviewed | rejected
    unreviewed_ai_count: int


class PatientSummary(BaseModel):
    """환자 필터 드롭다운용 요약"""
    id:   int
    name: str


class DashboardResponse(BaseModel):
    """GET /api/v1/dashboard 응답"""
    today:           str   # YYYY-MM-DD (조회 기준일)
    total_submitted: int
    pending_count:   int
    approved_count:  int
    total_patients:  int
    records:         List[DashboardRecordRow]
    patients:        List[PatientSummary]   # 환자 필터 드롭다운용
