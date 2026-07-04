"""
synth.py — 합성 CAPD 일일 기록 데이터 생성기

시드 고정 랜덤으로 (daily_data, exchange_records) 페어를 여러 날짜치 생성한다.
test_ai_parity.py(순수 함수 비교)와 test_analytics_endpoint.py(DB 통합)가
이 생성기를 공유해서, 같은 입력이면 항상 같은 기대값을 갖도록 보장한다.

daily_data / exchange_records의 키 구조는 ai/tools/data_engineering.py
build_daily_model_row()의 입력 규약을 그대로 따른다.
"""
import random
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

DailyPair = Tuple[Dict[str, Any], List[Dict[str, Any]]]


def gen_daily_pair(rng: random.Random, day: date, session_count: int = 4) -> DailyPair:
    """하루치 (daily_data, exchange_records) 합성."""
    weight = round(rng.uniform(55.0, 75.0), 1)
    sbp = rng.randint(110, 160)
    dbp = rng.randint(70, 100)
    glucose = round(rng.uniform(90.0, 180.0), 1)
    urine = rng.randint(0, 4)
    turbid = rng.random() < 0.05

    exchanges: List[Dict[str, Any]] = []
    minute = 7 * 60  # 07:00 시작
    total_uf = 0.0
    for i in range(1, session_count + 1):
        drainage = round(rng.uniform(1900.0, 2300.0), 1)
        infusion = round(rng.uniform(1900.0, 2200.0), 1)
        conc = rng.choice([1.5, 2.5, 4.25])
        uf = round(drainage - infusion, 1)
        total_uf += uf

        hh, mm = divmod(minute, 60)
        exchanges.append({
            "session_number": i,
            "exchange_time": f"{hh % 24:02d}:{mm:02d}",
            "drainage_volume": drainage,
            "infusion_concentration": conc,
            "infusion_weight": infusion,
            "ultrafiltration": uf,
        })
        minute += rng.randint(180, 300)

    daily_data = {
        "date": day.isoformat(),
        "weight": weight,
        "blood_pressure": f"{sbp}/{dbp}",
        "total_ultrafiltration": round(total_uf, 1),
        "turbid_peritoneal": turbid,
        "fasting_blood_glucose": glucose,
        "urine_count": urine,
        "note": None,
    }
    return daily_data, exchanges


def gen_synthetic_series(
    seed: int = 42,
    n_days: int = 40,
    start: date = date(2026, 1, 1),
) -> List[DailyPair]:
    """
    시드 고정 -> 재현 가능한 n_days치 (daily_data, exchange_records) 리스트.
    index 0 = 가장 과거, index -1 = 가장 최근("오늘").
    """
    rng = random.Random(seed)
    out: List[DailyPair] = []
    for i in range(n_days):
        day = start + timedelta(days=i)
        out.append(gen_daily_pair(rng, day))
    return out
