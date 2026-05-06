"""
AI 백그라운드 작업 서비스
- records.py / surveys.py 양쪽에서 공통으로 사용하는 AI 관련 백그라운드 로직
- 라우터 간 직접 import 방지를 위해 서비스 레이어로 분리
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.question import AIQuestion, AIQuestionStatus, AIQuestionType
from app.models.record import DailyRecord, RiskLevel

logger = logging.getLogger(__name__)

AI_SERVER_URL = "http://ai:8001"
MAX_AI_QUESTIONS = 5

# 백그라운드 AI 질문 생성 중인 record_id 추적 (중복 실행 방지)
# TODO: 서비스 확장 시 Redis 기반으로 교체 필요
_ai_in_progress: set = set()

# 백그라운드 AI 요약 생성 중인 record_id 추적 (중복 제출 방지)
# TODO: 서비스 확장 시 Redis 기반으로 교체 필요
_summary_in_progress: set = set()


# ── 과거 기록 추세 계산 ───────────────────────────────────────
def compute_historical_context(db: Session, patient_id: int, current_record_id: int) -> dict:
    """
    오늘 이전 기록을 최대 30일치 집계하여 추세 컨텍스트 반환.
    기록이 1개 이상이면 집계 결과를 반환 (트렌드 판단은 3개 이상일 때만 의미 있음).
    기록이 없으면 빈 dict 반환.
    TODO: 환자 수 증가 시 캐싱 또는 사전 집계 테이블 도입 필요
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
        return {}

    days = len(records)

    # 혈압 집계
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
            "trend": "상승" if len(bp_systolics) >= 3 and bp_systolics[0] > bp_systolics[-1] + 5
                     else "하강" if len(bp_systolics) >= 3 and bp_systolics[0] < bp_systolics[-1] - 5
                     else "안정",
        }

    # 체중 집계
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

    # UF량 주간 평균
    ufs = [(r.record_date, float(r.total_ultrafiltration)) for r in records if r.total_ultrafiltration is not None]
    uf_ctx = {}
    if ufs:
        weekly: list[list[float]] = [[], [], []]
        for rec_date, uf_val in ufs:
            days_ago = (datetime.now(timezone.utc).date() - rec_date).days
            week_idx = min(days_ago // 7, 2)
            weekly[week_idx].append(uf_val)
        weekly_avgs = [round(sum(w) / len(w)) for w in weekly if w]
        uf_trend = "감소" if len(weekly_avgs) >= 2 and weekly_avgs[0] < weekly_avgs[-1] - 100 \
                   else "증가" if len(weekly_avgs) >= 2 and weekly_avgs[0] > weekly_avgs[-1] + 100 \
                   else "안정"
        uf_ctx = {"weekly_avg": weekly_avgs, "trend": uf_trend}

    # 혈당 집계
    glucoses = [float(r.fasting_blood_glucose) for r in records if r.fasting_blood_glucose is not None]
    gl_ctx = {}
    if glucoses:
        gl_ctx = {
            "avg": round(sum(glucoses) / len(glucoses), 1),
            "max": max(glucoses),
        }

    # 위험도 이력
    risk_summary = {"urgent": 0, "caution": 0, "normal": 0}
    for r in records:
        if r.risk_level:
            key = r.risk_level.value if hasattr(r.risk_level, "value") else str(r.risk_level)
            if key in risk_summary:
                risk_summary[key] += 1

    return {
        "days": days,
        "bp": bp_ctx,
        "weight": wt_ctx,
        "uf": uf_ctx,
        "glucose": gl_ctx,
        "risk_summary": risk_summary,
    }


# ── AI 질문 생성 백그라운드 함수 ─────────────────────────────
def ai_question_background(
    record_id: int,
    patient_id: int,
    record_data: dict,
    rejected_keys: list,
    historical_context: dict = None,
    patient_profile: dict = None,
):
    """
    백그라운드에서 ai/ 서버의 /ai-questions/generate 호출
    Gemini가 KDIGO 기반으로 3~5개 질문 전담 생성 → DB 저장
    """
    db = SessionLocal()
    try:
        current_count = db.query(AIQuestion).filter(
            AIQuestion.daily_record_id == record_id
        ).count()

        if current_count >= MAX_AI_QUESTIONS:
            logger.info(f"record_id={record_id} 이미 질문 {current_count}개 — AI 서버 생략")
            return

        with httpx.Client(timeout=90.0) as client:
            resp = client.post(
                f"{AI_SERVER_URL}/ai-questions/generate",
                json={
                    "record_data":        record_data,
                    "rejected_keys":      rejected_keys,
                    "historical_context": historical_context or {},
                    "patient_profile":    patient_profile or {},
                },
            )
            resp.raise_for_status()
            ai_questions = resp.json().get("questions", [])

        added = 0
        for q_data in ai_questions:
            current_count = db.query(AIQuestion).filter(
                AIQuestion.daily_record_id == record_id
            ).count()
            if current_count >= MAX_AI_QUESTIONS:
                break
            q_type_str = q_data.get("question_type", "yes_no")
            try:
                q_type = AIQuestionType(q_type_str)
            except ValueError:
                q_type = AIQuestionType.yes_no
            db.add(AIQuestion(
                daily_record_id=record_id,
                patient_id=patient_id,
                question_text=q_data["question_text"],
                reason=q_data.get("reason"),
                question_type=q_type,
                options=json.dumps(q_data["options"], ensure_ascii=False) if q_data.get("options") is not None else None,
            ))
            added += 1

        db.commit()
        logger.info(f"AI 서버 질문 {added}개 저장 완료 (record_id={record_id})")

    except Exception as e:
        logger.warning(f"AI 서버 질문 생성 실패 (record_id={record_id}): {e}")
        db.rollback()
    finally:
        _ai_in_progress.discard(record_id)
        db.close()


# ── AI 요약 백그라운드 함수 ──────────────────────────────────
def summary_background(
    record_id: int,
    record_data: dict,
    common_qa: list,
    ai_survey_responses: list,
    historical_context: dict,
    patient_profile: dict,
):
    """백그라운드에서 AI 요약/위험도/EMR 생성 후 DB 저장"""
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
