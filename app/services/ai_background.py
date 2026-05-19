"""
AI 백그라운드 작업 서비스
- records.py / surveys.py 양쪽에서 공통으로 사용하는 AI 관련 백그라운드 로직
- 라우터 간 직접 import 방지를 위해 서비스 레이어로 분리

변경사항 (v3 — SSE 리팩토링):
- ai_question_background / _ai_in_progress 제거 → SSE 방식(surveys.py)으로 대체
- summary_background: 기존 유지 (AI 설문 제출 시 백그라운드 요약 생성)
- compute_historical_context: ExchangeRecord 포함한 raw historical_records 반환
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.question import AIQuestion, AIQuestionStatus, AIQuestionType
from app.models.record import DailyRecord, ExchangeRecord, RiskLevel

logger = logging.getLogger(__name__)

AI_SERVER_URL = settings.AI_SERVICE_URL
MAX_AI_QUESTIONS = 5

# 요약 중복 실행 방지 (TODO: 서비스 확장 시 Redis로 교체)
_summary_in_progress: set = set()


# ── 과거 기록 추세 계산 ───────────────────────────────────────────

def compute_historical_context(db: Session, patient_id: int, current_record_id: int) -> dict:
    """
    오늘 이전 기록을 최대 30일치 집계.

    Returns:
        {
            "context": {days, bp, weight, uf, glucose, risk_summary},  # 기존 단순 집계
            "historical_records": [                                      # ai/ 서버용 raw 데이터
                {date, weight, blood_pressure, ..., exchange_records: [...]},
                ...
            ]
        }
    """
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=30)

    records = (
        db.query(DailyRecord)
        .filter(
            DailyRecord.patient_id == patient_id,
            DailyRecord.id != current_record_id,
            DailyRecord.record_date >= cutoff,
        )
        .order_by(DailyRecord.record_date.desc())
        .all()
    )

    if len(records) < 1:
        return {"context": {}, "historical_records": []}

    days = len(records)

    # ── 기존 단순 집계 (하위 호환) ────────────────────────────────
    bp_systolics = []
    for r in records:
        try:
            bp_systolics.append(int(r.blood_pressure.split("/")[0]))
        except Exception:
            pass

    bp_ctx = {}
    if bp_systolics:
        bp_ctx = {
            "avg": str(round(sum(bp_systolics) / len(bp_systolics))),
            "max": str(max(bp_systolics)),
            "min": str(min(bp_systolics)),
            "trend": (
                "상승" if len(bp_systolics) >= 3 and bp_systolics[0] > bp_systolics[-1] + 5
                else "하강" if len(bp_systolics) >= 3 and bp_systolics[0] < bp_systolics[-1] - 5
                else "안정"
            ),
        }

    weights = [float(r.weight) for r in records if r.weight is not None]
    wt_ctx = {}
    if weights:
        avg_wt = round(sum(weights) / len(weights), 1)
        recent_7 = weights[:7]
        delta_7d = round(recent_7[0] - recent_7[-1], 1) if len(recent_7) >= 2 else 0.0
        wt_ctx = {
            "avg": avg_wt,
            "delta_7d": delta_7d,
            "trend": "증가" if delta_7d > 0.5 else "감소" if delta_7d < -0.5 else "안정",
        }

    ufs = [(r.record_date, float(r.total_ultrafiltration)) for r in records if r.total_ultrafiltration is not None]
    uf_ctx = {}
    if ufs:
        weekly: list[list[float]] = [[], [], []]
        for rec_date, uf_val in ufs:
            days_ago = (datetime.now(timezone.utc).date() - rec_date).days
            week_idx = min(days_ago // 7, 2)
            weekly[week_idx].append(uf_val)
        weekly_avgs = [round(sum(w) / len(w)) for w in weekly if w]
        uf_trend = (
            "감소" if len(weekly_avgs) >= 2 and weekly_avgs[0] < weekly_avgs[-1] - 100
            else "증가" if len(weekly_avgs) >= 2 and weekly_avgs[0] > weekly_avgs[-1] + 100
            else "안정"
        )
        uf_ctx = {"weekly_avg": weekly_avgs, "trend": uf_trend}

    glucoses = [float(r.fasting_blood_glucose) for r in records if r.fasting_blood_glucose is not None]
    gl_ctx = {}
    if glucoses:
        gl_ctx = {"avg": round(sum(glucoses) / len(glucoses), 1), "max": max(glucoses)}

    risk_summary = {"urgent": 0, "caution": 0, "normal": 0}
    for r in records:
        if r.risk_level:
            key = r.risk_level.value if hasattr(r.risk_level, "value") else str(r.risk_level)
            if key in risk_summary:
                risk_summary[key] += 1

    simple_context = {
        "days": days,
        "bp": bp_ctx,
        "weight": wt_ctx,
        "uf": uf_ctx,
        "glucose": gl_ctx,
        "risk_summary": risk_summary,
    }

    # ── ai/ 서버용 raw historical_records 구성 ────────────────────
    historical_records = []
    for r in records:
        # exchange_records 조회
        exchanges = (
            db.query(ExchangeRecord)
            .filter(ExchangeRecord.daily_record_id == r.id)
            .order_by(ExchangeRecord.session_number)
            .all()
        )
        exchange_list = [
            {
                "session_number":         ex.session_number,
                "exchange_time":          ex.exchange_time,
                "drainage_volume":        float(ex.drainage_volume) if ex.drainage_volume is not None else None,
                "infusion_concentration": float(ex.infusion_concentration) if ex.infusion_concentration is not None else None,
                "infusion_weight":        float(ex.infusion_weight) if ex.infusion_weight is not None else None,
                "ultrafiltration":        float(ex.ultrafiltration) if ex.ultrafiltration is not None else None,
            }
            for ex in exchanges
        ]
        historical_records.append({
            "date":                  str(r.record_date),
            "weight":                float(r.weight) if r.weight is not None else None,
            "blood_pressure":        r.blood_pressure,
            "total_ultrafiltration": float(r.total_ultrafiltration) if r.total_ultrafiltration is not None else None,
            "turbid_peritoneal":     r.turbid_peritoneal,
            "fasting_blood_glucose": float(r.fasting_blood_glucose) if r.fasting_blood_glucose is not None else None,
            "urine_count":           r.urine_count,
            "exchange_records":      exchange_list,
        })

    return {
        "context":            simple_context,
        "historical_records": historical_records,
    }


# ── AI 요약 백그라운드 함수 ─────────────────────────────────────────

def summary_background(
    record_id: int,
    record_data: dict,
    common_qa: list,
    ai_survey_responses: list,
    historical_context: dict,
    patient_profile: dict,
    historical_records: list[dict] = None,  # ai/ 서버용 raw 과거 기록
):
    """
    백그라운드에서 AI 요약/위험도/EMR 생성 후 DB 저장
    historical_records 전달 → ai/ 서버에서 analytics 후 summary_agent에 주입
    """
    db = SessionLocal()
    try:
        with httpx.Client(timeout=90.0) as client:
            resp = client.post(
                f"{AI_SERVER_URL}/summary",
                json={
                    "record_data":         record_data,
                    "common_qa":           common_qa,
                    "ai_survey_responses": ai_survey_responses,
                    "historical_context":  historical_context,
                    "patient_profile":     patient_profile,
                    "historical_records":  historical_records or [],
                },
            )
            resp.raise_for_status()
            result = resp.json()

        record = db.query(DailyRecord).filter(DailyRecord.id == record_id).first()
        if record:
            record.risk_level = RiskLevel(result["risk_level"])
            record.ai_summary = result["ai_summary"]
            record.emr_soap   = result["emr_soap"]
            db.commit()
            logger.info(f"AI summary saved (record_id={record_id}, risk={result['risk_level']})")

    except Exception as e:
        logger.error(f"AI summary background failed (record_id={record_id}): {e}")
        db.rollback()
    finally:
        _summary_in_progress.discard(record_id)
        db.close()
