"""
backfill_ai_summary.py
=======================
2026-06-26 커밋(e9f584c, vertexai→google-genai 전면 교체)에서 summary_agent.py의
`get_gemini_model` import가 누락돼, 그 이후 제출된 기록들이 전부 Gemini 호출 실패
(NameError) → `_fallback_triage()` 폴백 데이터로 저장됨. 2026-07-07 import 복구로
원인은 고쳤지만, 그 사이 이미 저장된 기록들의 emr_soap/ai_summary는 자동으로
재생성되지 않음 — 이 스크립트가 해당 기록들만 골라 AI 요약을 재실행한다.

대상 판별: emr_soap가 빈 값(NULL 또는 '')인 submitted/reviewed 기록.
  - `_fallback_triage()`는 emr_soap를 항상 ''로 남기고, 정상 Gemini 성공 경로는
    항상 비어있지 않은 SOAP 텍스트를 반환하므로 이 마커만으로 충분히 특정 가능.
  - 날짜 하드코딩 없이 이 마커 기준으로만 대상을 고름(더 견고함).

⚠️ 중요 — 새로 만들지 않는 것:
  - AI 질문 생성/재생성 안 함, 기존 survey_responses(공통질문·AI질문 답변)도
    그대로 재사용만 함 — 이미 있는 데이터로 "AI 요약 생성 단계"만 다시 실행.
  - 즉 이 스크립트는 records.py/surveys.py의 제출·설문 흐름을 전혀 안 건드림,
    surveys.py가 하던 마지막 한 단계(ai 서버 /summary 호출 + 결과 저장)만
    과거 기록에 대해 나중에 대신 실행하는 것.

historical_context 계산 시 주의 — "오늘" 기준점:
  - 원본 ai_background.compute_historical_context()는 호출 시점의 실제 현재
    시각을 "오늘"로 써서 cutoff·주간버킷을 계산함(제출 당일 호출이라 자연스럽게
    record_date ≈ 오늘이었음).
  - 이 스크립트는 한참 뒤에 실행되므로, 실제 지금 시각이 아니라 "그 기록의
    record_date"를 기준점으로 삼아 동일 로직을 재현함(그래야 당시와 동일한
    30일 컷오프·주간 버킷이 나옴). 또한 미래 데이터가 과거 기록의 historical
    context에 새어 들어가지 않도록 record_date 이하로만 조회(원본은 이 경계가
    자연히 지켜졌지만 여기선 명시적으로 조건을 건다).

실행 (Cloud Shell):
  pip install --user psycopg2-binary httpx
  DATABASE_URL='postgresql://...'  \
  AI_SERVICE_URL='https://capd-ai-7ywekqryvq-du.a.run.app' \
  python3 backfill_ai_summary.py            # 기본: dry-run, 대상 개수/목록만 출력
  python3 backfill_ai_summary.py --apply    # 실제 재생성 실행

멱등: emr_soap가 채워지면 그 기록은 더 이상 대상이 아니게 되므로 재실행해도 안전.
"""
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, date

import httpx
import psycopg2
import psycopg2.extras

DATABASE_URL = os.getenv("DATABASE_URL")
AI_SERVICE_URL = os.getenv("AI_SERVICE_URL", "https://capd-ai-7ywekqryvq-du.a.run.app")
APPLY = "--apply" in sys.argv

if not DATABASE_URL:
    print("❌ DATABASE_URL 환경변수가 필요합니다.")
    sys.exit(1)


def to_float(v):
    return float(v) if v is not None else None


def build_record_data(cur, record) -> dict:
    cur.execute(
        """
        SELECT session_number, exchange_time, drainage_volume,
               infusion_concentration, infusion_weight, ultrafiltration
        FROM exchange_records
        WHERE daily_record_id = %s
        ORDER BY session_number
        """,
        (record["id"],),
    )
    exchanges = [
        {
            "session_number": r["session_number"],
            "exchange_time": r["exchange_time"],
            "drainage_volume": to_float(r["drainage_volume"]),
            "infusion_concentration": to_float(r["infusion_concentration"]),
            "infusion_weight": to_float(r["infusion_weight"]),
            "ultrafiltration": to_float(r["ultrafiltration"]),
        }
        for r in cur.fetchall()
    ]
    return {
        "date": str(record["record_date"]),
        "blood_pressure": record["blood_pressure"],
        "weight": to_float(record["weight"]),
        "total_ultrafiltration": to_float(record["total_ultrafiltration"]),
        "fasting_blood_glucose": to_float(record["fasting_blood_glucose"]),
        "turbid_peritoneal": record["turbid_peritoneal"],
        "urine_count": record["urine_count"],
        "memo": record["memo"],
        "exchange_records": exchanges,
    }


def build_common_qa(cur, record) -> list:
    cur.execute(
        """
        SELECT DISTINCT cq.id, cq.question_text, cq.created_at
        FROM common_questions cq
        LEFT JOIN question_patient_assignments qpa
               ON qpa.question_id = cq.id AND qpa.patient_id = %s
        WHERE cq.is_active = TRUE
          AND (cq.target_all_patients = TRUE OR qpa.patient_id IS NOT NULL)
        ORDER BY cq.created_at ASC
        """,
        (record["patient_id"],),
    )
    common_qs = cur.fetchall()

    cur.execute(
        """
        SELECT question_id, question_type, choice, text_answer
        FROM survey_responses
        WHERE daily_record_id = %s
        """,
        (record["id"],),
    )
    resp_map = {(r["question_id"], r["question_type"]): r for r in cur.fetchall()}

    common_qa = []
    for q in common_qs:
        r = resp_map.get((q["id"], "common"))
        common_qa.append({
            "question_text": q["question_text"],
            "choice": r["choice"] if r else None,
            "text_answer": r["text_answer"] if r else None,
        })
    return common_qa, resp_map


def build_ai_survey_responses(cur, record, resp_map) -> list:
    cur.execute(
        """
        SELECT id, question_text, question_type
        FROM ai_questions
        WHERE daily_record_id = %s AND status != 'rejected_global'
        """,
        (record["id"],),
    )
    ai_qs = cur.fetchall()

    ai_survey_responses = []
    for q in ai_qs:
        r = resp_map.get((q["id"], "ai"))
        if not r:
            continue
        answer = r["text_answer"] or r["choice"] or "미응답"
        ai_survey_responses.append({
            "question_text": q["question_text"],
            "question_type": q["question_type"] or "yes_no",
            "answer": answer,
        })
    return ai_survey_responses


def compute_historical_context(cur, patient_id: int, current_record_id: int, as_of: date) -> dict:
    """ai_background.compute_historical_context() 재현.
    실제 '지금'이 아니라 as_of(백필 대상 기록의 record_date)를 기준점으로 사용."""
    cutoff = as_of - timedelta(days=30)

    cur.execute(
        """
        SELECT id, record_date, blood_pressure, weight, total_ultrafiltration,
               fasting_blood_glucose, turbid_peritoneal, urine_count, memo, risk_level
        FROM daily_records
        WHERE patient_id = %s AND id != %s
          AND record_date >= %s AND record_date <= %s
        ORDER BY record_date DESC
        """,
        (patient_id, current_record_id, cutoff, as_of),
    )
    records = cur.fetchall()

    if len(records) < 1:
        return {"context": {}, "historical_records": []}

    days = len(records)

    bp_systolics = []
    for r in records:
        try:
            bp_systolics.append(int(r["blood_pressure"].split("/")[0]))
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

    weights = [float(r["weight"]) for r in records if r["weight"] is not None]
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

    ufs = [(r["record_date"], float(r["total_ultrafiltration"])) for r in records if r["total_ultrafiltration"] is not None]
    uf_ctx = {}
    if ufs:
        weekly = [[], [], []]
        for rec_date, uf_val in ufs:
            days_ago = (as_of - rec_date).days
            week_idx = min(days_ago // 7, 2)
            weekly[week_idx].append(uf_val)
        weekly_avgs = [round(sum(w) / len(w)) for w in weekly if w]
        uf_trend = (
            "감소" if len(weekly_avgs) >= 2 and weekly_avgs[0] < weekly_avgs[-1] - 100
            else "증가" if len(weekly_avgs) >= 2 and weekly_avgs[0] > weekly_avgs[-1] + 100
            else "안정"
        )
        uf_ctx = {"weekly_avg": weekly_avgs, "trend": uf_trend}

    glucoses = [float(r["fasting_blood_glucose"]) for r in records if r["fasting_blood_glucose"] is not None]
    gl_ctx = {}
    if glucoses:
        gl_ctx = {"avg": round(sum(glucoses) / len(glucoses), 1), "max": max(glucoses)}

    risk_summary = {"urgent": 0, "caution": 0, "normal": 0}
    for r in records:
        key = r["risk_level"]
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

    record_ids = [r["id"] for r in records]
    exchanges_by_record = defaultdict(list)
    if record_ids:
        cur.execute(
            """
            SELECT daily_record_id, session_number, exchange_time, drainage_volume,
                   infusion_concentration, infusion_weight, ultrafiltration
            FROM exchange_records
            WHERE daily_record_id = ANY(%s)
            ORDER BY daily_record_id, session_number
            """,
            (record_ids,),
        )
        for ex in cur.fetchall():
            exchanges_by_record[ex["daily_record_id"]].append(ex)

    historical_records = []
    for r in records:
        exchange_list = [
            {
                "session_number": ex["session_number"],
                "exchange_time": ex["exchange_time"],
                "drainage_volume": to_float(ex["drainage_volume"]),
                "infusion_concentration": to_float(ex["infusion_concentration"]),
                "infusion_weight": to_float(ex["infusion_weight"]),
                "ultrafiltration": to_float(ex["ultrafiltration"]),
            }
            for ex in exchanges_by_record[r["id"]]
        ]
        historical_records.append({
            "date": str(r["record_date"]),
            "weight": to_float(r["weight"]),
            "blood_pressure": r["blood_pressure"],
            "total_ultrafiltration": to_float(r["total_ultrafiltration"]),
            "turbid_peritoneal": r["turbid_peritoneal"],
            "fasting_blood_glucose": to_float(r["fasting_blood_glucose"]),
            "urine_count": r["urine_count"],
            "note": r["memo"],
            "exchange_records": exchange_list,
        })

    return {"context": simple_context, "historical_records": historical_records}


def build_patient_profile(cur, patient_id: int) -> dict:
    cur.execute("SELECT self_memo FROM users WHERE id = %s", (patient_id,))
    row = cur.fetchone()
    self_memo = row["self_memo"] if row else None

    cur.execute(
        """
        SELECT content FROM patient_notes
        WHERE patient_id = %s
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (patient_id,),
    )
    note_row = cur.fetchone()
    doctor_note = note_row["content"] if note_row else None

    return {"self_memo": self_memo, "doctor_note": doctor_note}


def run():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        """
        SELECT id, patient_id, record_date, blood_pressure, weight,
               total_ultrafiltration, fasting_blood_glucose, turbid_peritoneal,
               urine_count, memo, status
        FROM daily_records
        WHERE status IN ('submitted', 'reviewed')
          AND (emr_soap IS NULL OR emr_soap = '')
        ORDER BY id
        """
    )
    targets = cur.fetchall()

    print(f"=== 대상 {len(targets)}건 (emr_soap 비어있는 submitted/reviewed 기록) ===")
    for t in targets:
        print(f"  - record_id={t['id']} patient_id={t['patient_id']} date={t['record_date']} status={t['status']}")

    if not targets:
        print("대상 없음 — 종료")
        return

    if not APPLY:
        print("\n(dry-run) 실제 재생성하려면 --apply 옵션을 붙여 다시 실행하세요.")
        return

    ok, fail = 0, []
    for t in targets:
        rid = t["id"]
        try:
            record_data = build_record_data(cur, t)
            common_qa, resp_map = build_common_qa(cur, t)
            ai_survey_responses = build_ai_survey_responses(cur, t, resp_map)
            hist = compute_historical_context(cur, t["patient_id"], rid, t["record_date"])
            patient_profile = build_patient_profile(cur, t["patient_id"])

            payload = {
                "record_data": record_data,
                "common_qa": common_qa,
                "ai_survey_responses": ai_survey_responses,
                "historical_context": hist["context"],
                "patient_profile": patient_profile,
                "historical_records": hist["historical_records"],
            }

            with httpx.Client(timeout=90.0) as client:
                resp = client.post(f"{AI_SERVICE_URL}/summary", json=payload)
                resp.raise_for_status()
                result = resp.json()

            cur.execute(
                """
                UPDATE daily_records
                SET risk_level = %s, ai_summary = %s, emr_soap = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (result["risk_level"], result["ai_summary"], result["emr_soap"], rid),
            )
            conn.commit()
            ok += 1
            print(f"  ✅ record_id={rid} 완료 (risk={result['risk_level']})")

        except Exception as e:
            conn.rollback()
            fail.append(rid)
            print(f"  ❌ record_id={rid} 실패: {e}")

        time.sleep(1.5)  # Gemini RPM 여유

    print(f"\n=== 완료 — 성공 {ok}건 / 실패 {len(fail)}건 ===")
    if fail:
        print(f"실패 record_id: {fail}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    run()
