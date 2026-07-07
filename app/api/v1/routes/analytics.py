"""
analytics.py — 온디맨드 분석 리포트 API

의사가 "분석 리포트" 화면을 열면 배포된 backend가 즉석으로
app/services/analytics_engine.py(ai/tools 포팅본)를 돌려 결과를 반환한다.
ai/ 서버 호출 없음 — backend 단독 즉석 계산(speed layer).

계산 결과는 Silver(patient_daily_metrics)/Gold(patient_daily_analytics) 캐시
테이블에 upsert 해 둔다. 캐시 upsert 실패는 응답에 영향을 주지 않음(best-effort) —
이 두 테이블은 나중에 Airflow 배치가 전체 환자에 대해 매일 채워 넣는 대상
테이블이기도 하다.

같은 (patient_id, record_date)에 이미 계산된 Gold 캐시가 있으면 재계산 없이
그대로 반환한다(_read_cache). 기록 값 자체의 신선도 체크는 필요 없음 —
submitted/reviewed 기록의 수치는 draft 상태에서만 수정 가능하므로
(records.py update_record) 한 번 이 캐시에 들어온 값은 절대 바뀌지 않는다.
다만 요청된 window(7/30/90일)에 따라 실제로 쓰인 과거 기록 개수가 다를 수 있어,
그 개수가 캐시 계산 당시와 다르면 재계산한다(_read_cache의 window_days 비교 참고).
응답의 "source" 필드로 cache/on_demand 구분.

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

def _upsert_cache(db: Session, patient_id: int, record_date, window_days: int, today_row: Dict[str, Any], result: Dict[str, Any]) -> None:
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
                    patient_id, record_date, window_days,
                    trend_json, anomaly_json, correlation_json, eda_json,
                    has_anomaly, anomaly_attrs, computed_at
                ) VALUES (
                    :patient_id, :record_date, :window_days,
                    CAST(:trend_json AS JSONB), CAST(:anomaly_json AS JSONB),
                    CAST(:correlation_json AS JSONB), CAST(:eda_json AS JSONB),
                    :has_anomaly, :anomaly_attrs, NOW()
                )
                ON CONFLICT (patient_id, record_date, window_days) DO UPDATE SET
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
                "window_days": window_days,
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


# ── Gold 캐시 조회 ────────────────────────────────────────────

def _read_cache(
    db: Session, patient_id: int, record_date, window_days: int
) -> Optional[Dict[str, Any]]:
    """
    patient_daily_analytics(Gold)에 (patient_id, record_date, window_days) 캐시가
    있으면 반환.

    기록 값 자체는 신선도 체크가 필요 없음 — submitted/reviewed 상태로 들어온 기록의
    수치(체중/혈압/UF 등)는 approve/revert(상태만 변경)로도 절대 바뀌지 않으므로
    (수정은 draft 상태에서만 가능, records.py update_record 참고).

    window_days는 요청받은 window(7/30/90일 선택)에 따라 실제로 사용된 과거 기록
    개수(historical_rows 길이) — 캐시 키에 포함시켜서 7/30/90 전환할 때 서로 다른
    행에 저장되므로 무효화 없이 각자 캐시를 유지한다(예전엔 (patient_id, record_date)
    딱 1행뿐이라 window를 바꿀 때마다 직전 캐시를 밀어내던 문제, CLAUDE.md "알려진
    제한사항" 참고 — backend/scripts/migrate_analytics_cache_window.py로 스키마 변경).
    """
    row = db.execute(
        text("""
            SELECT trend_json, anomaly_json, correlation_json, eda_json,
                   has_anomaly, anomaly_attrs
            FROM patient_daily_analytics
            WHERE patient_id = :patient_id AND record_date = :record_date
              AND window_days = :window_days
        """),
        {"patient_id": patient_id, "record_date": record_date, "window_days": window_days},
    ).first()
    if row is None:
        return None
    return {
        "trend_analysis":        row.trend_json,
        "anomaly_detection":     row.anomaly_json,
        "attribute_correlation": row.correlation_json,
        "eda":                   row.eda_json,
        "has_anomaly":           row.has_anomaly,
        "anomaly_attrs":         row.anomaly_attrs,
    }


# ── 추세 카드 미니차트용 일별 시계열 ──────────────────────────────

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

    cached = _read_cache(db, patient_id, today_record.record_date, len(historical_rows))
    if cached is not None:
        result = cached
        source = "cache"
    else:
        result = run_all_tasks(today_row, historical_rows, window=window)
        _upsert_cache(db, patient_id, today_record.record_date, len(historical_rows), today_row, result)
        source = "on_demand"

    daily_series = _build_daily_series(today_row, historical_rows)

    return {
        "patient_id":   patient_id,
        "patient_name": patient.name,
        "record_date":  today_record.record_date.isoformat(),
        "window_days":  len(historical_rows),
        "source":       source,
        "daily_series": daily_series,
        **result,
    }
