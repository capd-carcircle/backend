"""
analytics.py — 온디맨드 분석 리포트 API (AB180 전환설계 9-2/9-4, 3단계)

의사가 "분석 리포트" 화면을 열면(추후 4단계 frontend) 배포된 backend가
즉석으로 app/services/analytics_engine.py(ai/tools 포팅본)를 돌려 결과를 반환한다.
ai/ 서버 호출 없음 — backend 단독 즉석 계산(speed layer).

계산 결과는 Silver(patient_daily_metrics)/Gold(patient_daily_analytics) 캐시
테이블에 upsert 해 둔다. 캐시 upsert 실패는 응답에 영향을 주지 않음(best-effort) —
이 두 테이블은 나중에 capd-pipeline(Airflow) 배치가 전체 환자에 대해 매일
채워 넣는 대상 테이블이기도 하다 (9-1 참고).

접근 권한 로직은 patients.py의 담당 이력(patient_doctor_assignments) 규칙을 재사용.
"""
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, text
from sqlalchemy.orm import Session

from app.api.v1.routes.patients import _get_assignment, _require_doctor
from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.record import DailyRecord, RecordStatus
from app.models.user import User, UserRole
from app.services.analytics_engine import TREND_ATTRS, build_daily_model_row, run_all_tasks

router = APIRouter(prefix="/analytics", tags=["분석 리포트"])


# ── DB row → analytics_engine 입력 변환 ──────────────────────────

def _record_to_daily_data(r: DailyRecord) -> Dict[str, Any]:
    return {
        "record_date":           r.record_date.isoformat(),
        "weight":                float(r.weight) if r.weight is not None else None,
        "blood_pressure":        r.blood_pressure,
        "total_ultrafiltration": float(r.total_ultrafiltration) if r.total_ultrafiltration is not None else None,
        "turbid_peritoneal":     r.turbid_peritoneal,
        "fasting_blood_glucose": float(r.fasting_blood_glucose) if r.fasting_blood_glucose is not None else None,
        "urine_count":           r.urine_count,
        "note":                  r.memo,
    }


def _record_to_exchanges(r: DailyRecord) -> List[Dict[str, Any]]:
    return [
        {
            "session_number":         ex.session_number,
            "exchange_time":          ex.exchange_time,
            "drainage_volume":        float(ex.drainage_volume) if ex.drainage_volume is not None else None,
            "infusion_concentration": float(ex.infusion_concentration) if ex.infusion_concentration is not None else None,
            "infusion_weight":        float(ex.infusion_weight) if ex.infusion_weight is not None else None,
            "ultrafiltration":        float(ex.ultrafiltration) if ex.ultrafiltration is not None else None,
        }
        for ex in r.exchange_records
    ]


# ── Silver/Gold 캐시 upsert (best-effort) ────────────────────────

def _upsert_cache(db: Session, patient_id: int, record_date, today_row: Dict[str, Any], result: Dict[str, Any]) -> None:
    try:
        db.execute(
            text("""
                INSERT INTO patient_daily_metrics (
                    patient_id, record_date,
                    exchange_count, missing_exchange_slots, drain_sum_g, infused_sum_g,
                    recorded_uf_sum_g, calculated_uf_sum_g, uf_min_g, uf_std_g,
                    dwell_mean_minutes, dwell_std_minutes, concentration_max,
                    reported_total_uf_g, uf_discrepancy_g,
                    body_weight_kg, fasting_blood_sugar, urination_count, cloudy_dialysate,
                    systolic_bp, diastolic_bp, pulse_pressure, mean_arterial_pressure,
                    note, updated_at
                ) VALUES (
                    :patient_id, :record_date,
                    :exchange_count, :missing_exchange_slots, :drain_sum_g, :infused_sum_g,
                    :recorded_uf_sum_g, :calculated_uf_sum_g, :uf_min_g, :uf_std_g,
                    :dwell_mean_minutes, :dwell_std_minutes, :concentration_max,
                    :reported_total_uf_g, :uf_discrepancy_g,
                    :body_weight_kg, :fasting_blood_sugar, :urination_count, :cloudy_dialysate,
                    :systolic_bp, :diastolic_bp, :pulse_pressure, :mean_arterial_pressure,
                    :note, NOW()
                )
                ON CONFLICT (patient_id, record_date) DO UPDATE SET
                    exchange_count          = EXCLUDED.exchange_count,
                    missing_exchange_slots  = EXCLUDED.missing_exchange_slots,
                    drain_sum_g             = EXCLUDED.drain_sum_g,
                    infused_sum_g           = EXCLUDED.infused_sum_g,
                    recorded_uf_sum_g       = EXCLUDED.recorded_uf_sum_g,
                    calculated_uf_sum_g     = EXCLUDED.calculated_uf_sum_g,
                    uf_min_g                = EXCLUDED.uf_min_g,
                    uf_std_g                = EXCLUDED.uf_std_g,
                    dwell_mean_minutes      = EXCLUDED.dwell_mean_minutes,
                    dwell_std_minutes       = EXCLUDED.dwell_std_minutes,
                    concentration_max       = EXCLUDED.concentration_max,
                    reported_total_uf_g     = EXCLUDED.reported_total_uf_g,
                    uf_discrepancy_g        = EXCLUDED.uf_discrepancy_g,
                    body_weight_kg          = EXCLUDED.body_weight_kg,
                    fasting_blood_sugar     = EXCLUDED.fasting_blood_sugar,
                    urination_count         = EXCLUDED.urination_count,
                    cloudy_dialysate        = EXCLUDED.cloudy_dialysate,
                    systolic_bp             = EXCLUDED.systolic_bp,
                    diastolic_bp            = EXCLUDED.diastolic_bp,
                    pulse_pressure          = EXCLUDED.pulse_pressure,
                    mean_arterial_pressure  = EXCLUDED.mean_arterial_pressure,
                    note                    = EXCLUDED.note,
                    updated_at              = NOW()
            """),
            {
                "patient_id": patient_id,
                "record_date": record_date,
                "exchange_count": today_row.get("exchange_count"),
                "missing_exchange_slots": today_row.get("missing_exchange_slots"),
                "drain_sum_g": today_row.get("drain_sum_g"),
                "infused_sum_g": today_row.get("infused_sum_g"),
                "recorded_uf_sum_g": today_row.get("recorded_uf_sum_g"),
                "calculated_uf_sum_g": today_row.get("calculated_uf_sum_g"),
                "uf_min_g": today_row.get("uf_min_g"),
                "uf_std_g": today_row.get("uf_std_g"),
                "dwell_mean_minutes": today_row.get("dwell_mean_minutes"),
                "dwell_std_minutes": today_row.get("dwell_std_minutes"),
                "concentration_max": today_row.get("concentration_max"),
                "reported_total_uf_g": today_row.get("reported_total_uf_g"),
                "uf_discrepancy_g": today_row.get("uf_discrepancy_g"),
                "body_weight_kg": today_row.get("body_weight_kg"),
                "fasting_blood_sugar": today_row.get("fasting_blood_sugar"),
                "urination_count": today_row.get("urination_count"),
                "cloudy_dialysate": today_row.get("cloudy_dialysate"),
                "systolic_bp": today_row.get("systolic_bp"),
                "diastolic_bp": today_row.get("diastolic_bp"),
                "pulse_pressure": today_row.get("pulse_pressure"),
                "mean_arterial_pressure": today_row.get("mean_arterial_pressure"),
                "note": today_row.get("note"),
            },
        )

        db.execute(
            text("""
                INSERT INTO patient_daily_analytics (
                    patient_id, record_date,
                    trend_json, anomaly_json, correlation_json, eda_json,
                    has_anomaly, anomaly_attrs, computed_at
                ) VALUES (
                    :patient_id, :record_date,
                    CAST(:trend_json AS JSONB), CAST(:anomaly_json AS JSONB),
                    CAST(:correlation_json AS JSONB), CAST(:eda_json AS JSONB),
                    :has_anomaly, :anomaly_attrs, NOW()
                )
                ON CONFLICT (patient_id, record_date) DO UPDATE SET
                    trend_json       = EXCLUDED.trend_json,
                    anomaly_json     = EXCLUDED.anomaly_json,
                    correlation_json = EXCLUDED.correlation_json,
                    eda_json         = EXCLUDED.eda_json,
                    has_anomaly      = EXCLUDED.has_anomaly,
                    anomaly_attrs    = EXCLUDED.anomaly_attrs,
                    computed_at      = NOW()
            """),
            {
                "patient_id": patient_id,
                "record_date": record_date,
                "trend_json": json.dumps(result.get("trend_analysis"), ensure_ascii=False),
                "anomaly_json": json.dumps(result.get("anomaly_detection"), ensure_ascii=False),
                "correlation_json": json.dumps(result.get("attribute_correlation"), ensure_ascii=False),
                "eda_json": json.dumps(result.get("eda"), ensure_ascii=False),
                "has_anomaly": result.get("has_anomaly", False),
                "anomaly_attrs": result.get("anomaly_attrs", []),
            },
        )
        db.commit()
    except Exception:
        # 캐시 실패는 무시 — 온디맨드 응답 자체는 정상 반환 (best-effort)
        db.rollback()


# ── 추세 카드 미니차트용 일별 시계열 (4.5단계 ①) ──────────────────

def _build_daily_series(
    today_row: Dict[str, Any], historical_rows: List[Dict[str, Any]]
) -> Dict[str, List[Dict[str, Any]]]:
    """
    TREND_ATTRS 각각에 대해 {date, value} 점들을 날짜 오름차순으로 반환.
    프론트 추세 카드의 미니 차트(최근 N일 추이) 렌더링용.
    오늘 값도 포함(가장 마지막 점) — 이상치 마커는 프론트에서
    anomaly_detection.results[attr].is_anomaly 로 오늘 점만 표시.
    """
    all_rows = [today_row] + historical_rows
    series: Dict[str, List[Dict[str, Any]]] = {}
    for attr in TREND_ATTRS:
        pts = [
            {"date": r.get("date"), "value": r.get(attr)}
            for r in all_rows
            if r.get(attr) is not None and r.get("date")
        ]
        pts.sort(key=lambda p: p["date"])
        if len(pts) >= 2:
            series[attr] = pts
    return series


# ── 엔드포인트 ────────────────────────────────────────────────

@router.get(
    "/patients/{patient_id}",
    summary="환자 분석 리포트 (온디맨드, Trend/Anomaly/Correlation/EDA)",
)
def get_patient_analytics(
    patient_id: int,
    window: int = Query(30, ge=7, le=90, description="과거 며칠치를 baseline으로 쓸지"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    _require_doctor(current_user)

    patient = db.query(User).filter(User.id == patient_id, User.role == UserRole.patient).first()
    if not patient:
        raise HTTPException(status_code=404, detail="환자를 찾을 수 없습니다.")

    assignment = _get_assignment(db, current_user.id, patient_id)
    has_access = assignment is not None or patient.doctor_id == current_user.id
    if not has_access:
        raise HTTPException(status_code=403, detail="접근 권한이 없습니다.")

    # 과거 담당이면 담당 기간(~ended_at) 내 기록만
    records_q = db.query(DailyRecord).filter(
        DailyRecord.patient_id == patient_id,
        DailyRecord.status.in_([RecordStatus.submitted, RecordStatus.reviewed]),
    )
    is_current = assignment is None or assignment.ended_at is None
    if not is_current and assignment and assignment.ended_at:
        records_q = records_q.filter(DailyRecord.record_date <= assignment.ended_at.date())

    records = (
        records_q.order_by(desc(DailyRecord.record_date))
        .limit(window + 1)
        .all()
    )
    if not records:
        raise HTTPException(status_code=404, detail="분석할 제출/승인 기록이 없습니다.")

    today_record, *historical_records = records

    today_row = build_daily_model_row(
        _record_to_daily_data(today_record), _record_to_exchanges(today_record)
    )
    historical_rows = [
        build_daily_model_row(_record_to_daily_data(r), _record_to_exchanges(r))
        for r in historical_records
    ]

    result = run_all_tasks(today_row, historical_rows)

    _upsert_cache(db, patient_id, today_record.record_date, today_row, result)

    daily_series = _build_daily_series(today_row, historical_rows)

    return {
        "patient_id":   patient_id,
        "patient_name": patient.name,
        "record_date":  today_record.record_date.isoformat(),
        "window_days":  len(historical_rows),
        "source":       "on_demand",
        "daily_series": daily_series,
        **result,
    }
