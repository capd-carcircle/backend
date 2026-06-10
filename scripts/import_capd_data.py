"""
CAPD 더미 데이터 임포트 스크립트
===================================
synthetic_capd_2025 폴더 구조를 DB에 삽입한다.

지원 포맷
---------
1. CSV 모드  : daily_records.csv + exchange_sessions.csv 가 존재할 때 (권장)
2. JSON 모드 : patient_XXX/YYYY-MM-DD.json 파일만 존재할 때 (자동 감지)

실행 예시
---------
# 기본 (CSV 모드, 2025-12-01 ~ 2025-12-31, 비밀번호 capd1234)
python backend/scripts/import_capd_data.py --data-dir ./synthetic_capd_2025

# 날짜 범위 지정
python backend/scripts/import_capd_data.py --data-dir ./synthetic_capd_2025 \
    --start 2025-11-01 --end 2025-11-30

# 기존 더미 데이터 초기화 후 재삽입
python backend/scripts/import_capd_data.py --data-dir ./synthetic_capd_2025 --clear

환경변수
--------
DATABASE_URL (없으면 기본값: postgresql://capd_user:capd_pass@localhost:5432/capd)
"""

import argparse
import csv
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import bcrypt
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv
load_dotenv()

# ── 로깅 ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── 기본값 ────────────────────────────────────────────────────────────────────
DEFAULT_DB_URL    = "postgresql://capd_user:capd_pass@localhost:5432/capd"
DEFAULT_PASSWORD  = "12345678"
DEFAULT_START     = date(2025, 12, 1)
DEFAULT_END       = date(2025, 12, 31)


# ═══════════════════════════════════════════════════════════════════════════════
# 유틸
# ═══════════════════════════════════════════════════════════════════════════════

def _hash(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _in_range(d: date, start: date, end: date) -> bool:
    return start <= d <= end


def _to_bool(v) -> bool:
    """'Y'/'N'/0/1/'0'/'1'/True/False → bool"""
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return bool(v)
    return str(v).strip().upper() in ("Y", "1", "TRUE")


def _to_float(v) -> float | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_int(v) -> int | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# 환자 계정 생성
# ═══════════════════════════════════════════════════════════════════════════════

def ensure_patients(db, patients_index: list, password: str, data_dir: Path) -> dict[str, int]:
    """
    patients_index 목록을 보고 DB에 없는 계정만 생성.
    profile.json에서 성별·나이를 읽어 birth_date·gender·self_memo도 함께 저장.
    반환: { "patient_001": user_id, ... }
    """
    mapping: dict[str, int] = {}

    for p in patients_index:
        pid   = p["patient_id"]                          # "patient_001"
        num_part = pid.replace("patient_", "").zfill(8)  # "001" → "00000001"
        phone_number = f"010{num_part}"                  # "01000000001"
        name  = f"환자 {pid.replace('patient_', '')}"    # "환자 001"

        # profile.json에서 성별·나이·동반질환 읽기
        profile_path = data_dir / pid / "profile.json"
        gender = "m"
        birth_date = None
        self_memo = None
        if profile_path.exists():
            with open(profile_path, encoding="utf-8") as f:
                prof = json.load(f)
            gender = "f" if str(prof.get("sex", "M")).upper() == "F" else "m"
            age = prof.get("age")
            if age:
                birth_year = 2025 - int(age)
                birth_date = f"{birth_year}-01-01"
            comorbidities = prof.get("comorbidities", [])
            if comorbidities:
                self_memo = "동반질환: " + ", ".join(comorbidities)

        row = db.execute(
            text("SELECT id FROM users WHERE phone_number = :p"),
            {"p": phone_number},
        ).fetchone()

        if row:
            mapping[pid] = row[0]
            log.info(f"  기존 계정 사용: {phone_number} (id={row[0]})")
        else:
            result = db.execute(
                text("""
                    INSERT INTO users
                        (phone_number, password_hash, name, birth_date, gender, self_memo,
                         role, is_active, created_at, updated_at)
                    VALUES
                        (:phone_number, :pw, :name, :birth_date, :gender, :self_memo,
                         'patient', true, now(), now())
                    RETURNING id
                """),
                {
                    "phone_number": phone_number,
                    "pw": _hash(password),
                    "name": name,
                    "birth_date": birth_date,
                    "gender": gender,
                    "self_memo": self_memo,
                },
            )
            new_id = result.fetchone()[0]
            mapping[pid] = new_id
            log.info(f"  신규 계정 생성: {phone_number} / {gender} / {birth_date} (id={new_id})")

    db.commit()
    return mapping


# ═══════════════════════════════════════════════════════════════════════════════
# CSV 모드 임포트
# ═══════════════════════════════════════════════════════════════════════════════

def import_from_csv(db, data_dir: Path, patient_map: dict, start: date, end: date,
                    only_patients: set | None = None) -> int:
    """
    daily_records.csv + exchange_sessions.csv 기반 임포트.
    only_patients: {"patient_001", ...} 지정 시 해당 환자만 임포트 (None이면 전체)
    반환: 삽입된 DailyRecord 수
    """
    daily_csv    = data_dir / "daily_records.csv"
    exchange_csv = data_dir / "exchange_sessions.csv"

    if not daily_csv.exists():
        raise FileNotFoundError(f"daily_records.csv 없음: {daily_csv}")

    # ── exchange_sessions 미리 읽어서 (patient_id, date) → [session rows] 맵 구성 ──
    ex_map: dict[tuple, list] = {}
    if exchange_csv.exists():
        with open(exchange_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if _to_int(row.get("performed", 1)) == 0:
                    continue          # 미수행 세션 제외
                key = (row["patient_id"], row["date"])
                ex_map.setdefault(key, []).append(row)
    else:
        log.warning("exchange_sessions.csv 없음 — 교환 기록 없이 임포트")

    inserted = 0
    with open(daily_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pid       = row["patient_id"]
            rec_date  = _parse_date(row["date"])

            if pid not in patient_map:
                continue
            if only_patients and pid not in only_patients:
                continue
            if not _in_range(rec_date, start, end):
                continue

            user_id = patient_map[pid]

            # 중복 체크
            exists = db.execute(
                text("SELECT 1 FROM daily_records WHERE patient_id=:u AND record_date=:d"),
                {"u": user_id, "d": rec_date},
            ).fetchone()
            if exists:
                log.debug(f"  SKIP 중복: {pid} {rec_date}")
                continue

            # DailyRecord 삽입
            result = db.execute(
                text("""
                    INSERT INTO daily_records
                        (patient_id, record_date, turbid_peritoneal, weight,
                         blood_pressure, urine_count, total_ultrafiltration,
                         fasting_blood_glucose, memo,
                         status, submitted_at, created_at, updated_at)
                    VALUES
                        (:pid, :rd, :turb, :wt,
                         :bp, :uc, :tuf,
                         :fbg, :memo,
                         'submitted', now(), now(), now())
                    RETURNING id
                """),
                {
                    "pid":  user_id,
                    "rd":   rec_date,
                    "turb": _to_bool(row.get("cloudy_dialysate", 0)),
                    "wt":   _to_float(row.get("body_weight_kg")),
                    "bp":   row.get("blood_pressure_mmhg") or None,
                    "uc":   _to_int(row.get("urination_count")),
                    "tuf":  _to_float(row.get("total_ultrafiltration_g")),
                    "fbg":  _to_float(row.get("fasting_blood_sugar_mg_dl")),
                    "memo": row.get("notes") or None,
                },
            )
            record_id = result.fetchone()[0]

            # ExchangeRecord 삽입
            sessions = ex_map.get((pid, row["date"]), [])
            for s in sessions:
                db.execute(
                    text("""
                        INSERT INTO exchange_records
                            (daily_record_id, session_number, exchange_time,
                             drainage_volume, infusion_concentration,
                             infusion_weight, ultrafiltration, created_at)
                        VALUES
                            (:rid, :sn, :et,
                             :dv, :ic,
                             :iw, :uf, now())
                    """),
                    {
                        "rid": record_id,
                        "sn":  _to_int(s["session_number"]),
                        "et":  s.get("exchange_time") or None,
                        "dv":  _to_float(s.get("drain_volume_g")),
                        "ic":  _to_float(s.get("dialysate_concentration_percent")),
                        "iw":  _to_float(s.get("infused_fluid_weight_g")),
                        "uf":  _to_float(s.get("ultrafiltration_g")),
                    },
                )

            inserted += 1

    db.commit()
    return inserted


# ═══════════════════════════════════════════════════════════════════════════════
# JSON 모드 임포트
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_day_json(json_path: Path) -> dict | None:
    """
    patient_XXX/YYYY-MM-DD.json 파싱.
    배열 마지막 요소가 요약 dict, 나머지가 교환 기록 배열.
    반환: { "summary": {...}, "sessions": [...] }
    """
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list) or len(data) == 0:
        return None

    # 마지막 요소 = 요약 (cloudy_dialysate, body_weight_kg, ... 키 포함)
    summary = data[-1] if isinstance(data[-1], dict) and "date" in data[-1] else None
    if summary is None:
        return None

    # 나머지 = 교환 기록 (category 키 기준으로 피벗)
    cat_rows = [r for r in data[:-1] if isinstance(r, dict) and "category" in r]

    # 카테고리별 dict 구성
    cats: dict[str, dict] = {}
    for r in cat_rows:
        cats[r["category"]] = {k: v for k, v in r.items() if k != "category"}

    sessions = []
    for sn in range(1, 6):
        sn_str = str(sn)
        et  = cats.get("exchange_time", {}).get(sn_str, "")
        dv  = cats.get("drain_volume_g", {}).get(sn_str, "")
        ic  = cats.get("dialysate_concentration_percent", {}).get(sn_str, "")
        iw  = cats.get("infused_fluid_weight_g", {}).get(sn_str, "")
        uf  = cats.get("ultrafiltration_g", {}).get(sn_str, "")

        if not et:              # exchange_time 없으면 미수행 세션
            continue

        sessions.append({
            "session_number": sn,
            "exchange_time":  et or None,
            "drain_volume_g": _to_float(dv),
            "dialysate_concentration_percent": _to_float(ic),
            "infused_fluid_weight_g": _to_float(iw),
            "ultrafiltration_g": _to_float(uf),
        })

    return {"summary": summary, "sessions": sessions}


def import_from_json(db, data_dir: Path, patient_map: dict, start: date, end: date) -> int:
    """
    patient_XXX/YYYY-MM-DD.json 파일 기반 임포트.
    반환: 삽입된 DailyRecord 수
    """
    inserted = 0

    for pid, user_id in patient_map.items():
        patient_dir = data_dir / pid
        if not patient_dir.is_dir():
            log.warning(f"  폴더 없음: {patient_dir}")
            continue

        for json_file in sorted(patient_dir.glob("????-??-??.json")):
            rec_date = _parse_date(json_file.stem)
            if not _in_range(rec_date, start, end):
                continue

            parsed = _parse_day_json(json_file)
            if parsed is None:
                log.warning(f"  파싱 실패: {json_file}")
                continue

            s = parsed["summary"]

            # 중복 체크
            exists = db.execute(
                text("SELECT 1 FROM daily_records WHERE patient_id=:u AND record_date=:d"),
                {"u": user_id, "d": rec_date},
            ).fetchone()
            if exists:
                continue

            result = db.execute(
                text("""
                    INSERT INTO daily_records
                        (patient_id, record_date, turbid_peritoneal, weight,
                         blood_pressure, urine_count, total_ultrafiltration,
                         fasting_blood_glucose, memo,
                         status, submitted_at, created_at, updated_at)
                    VALUES
                        (:pid, :rd, :turb, :wt,
                         :bp, :uc, :tuf,
                         :fbg, :memo,
                         'submitted', now(), now(), now())
                    RETURNING id
                """),
                {
                    "pid":  user_id,
                    "rd":   rec_date,
                    "turb": _to_bool(s.get("cloudy_dialysate", "N")),
                    "wt":   _to_float(s.get("body_weight_kg")),
                    "bp":   s.get("blood_pressure_mmhg") or None,
                    "uc":   _to_int(s.get("urination_count")),
                    "tuf":  _to_float(s.get("total_ultrafiltration_g")),
                    "fbg":  _to_float(s.get("fasting_blood_sugar_mg_dl")),
                    "memo": s.get("notes") or None,
                },
            )
            record_id = result.fetchone()[0]

            for sess in parsed["sessions"]:
                db.execute(
                    text("""
                        INSERT INTO exchange_records
                            (daily_record_id, session_number, exchange_time,
                             drainage_volume, infusion_concentration,
                             infusion_weight, ultrafiltration, created_at)
                        VALUES
                            (:rid, :sn, :et, :dv, :ic, :iw, :uf, now())
                    """),
                    {
                        "rid": record_id,
                        "sn":  sess["session_number"],
                        "et":  sess["exchange_time"],
                        "dv":  sess["drain_volume_g"],
                        "ic":  sess["dialysate_concentration_percent"],
                        "iw":  sess["infused_fluid_weight_g"],
                        "uf":  sess["ultrafiltration_g"],
                    },
                )

            inserted += 1

    db.commit()
    return inserted


# ═══════════════════════════════════════════════════════════════════════════════
# 초기화
# ═══════════════════════════════════════════════════════════════════════════════

def clear_dummy_records(db, patient_map: dict):
    """더미 환자 기록만 삭제 (기존 테스트 계정 기록은 보존)"""
    user_ids = list(patient_map.values())
    if not user_ids:
        return
    placeholders = ",".join(str(i) for i in user_ids)

    db.execute(text(f"""
        DELETE FROM survey_responses
        WHERE daily_record_id IN (
            SELECT id FROM daily_records WHERE patient_id IN ({placeholders})
        )
    """))
    db.execute(text(f"""
        DELETE FROM ai_questions
        WHERE daily_record_id IN (
            SELECT id FROM daily_records WHERE patient_id IN ({placeholders})
        )
    """))
    db.execute(text(f"""
        DELETE FROM exchange_records
        WHERE daily_record_id IN (
            SELECT id FROM daily_records WHERE patient_id IN ({placeholders})
        )
    """))
    db.execute(text(f"DELETE FROM daily_records WHERE patient_id IN ({placeholders})"))
    db.commit()
    log.info("기존 더미 기록 초기화 완료")


# ═══════════════════════════════════════════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="CAPD 더미 데이터 임포트")
    parser.add_argument(
        "--data-dir", required=True,
        help="synthetic_capd_2025 폴더 경로 (patients_index.json이 있는 위치)"
    )
    parser.add_argument("--start", default=str(DEFAULT_START), help="시작 날짜 YYYY-MM-DD")
    parser.add_argument("--end",   default=str(DEFAULT_END),   help="종료 날짜 YYYY-MM-DD")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="생성할 환자 계정 비밀번호")
    parser.add_argument(
        "--clear", action="store_true",
        help="임포트 전 더미 환자 기록 초기화 (계정은 유지)"
    )
    parser.add_argument(
        "--patients", default=None,
        help="임포트할 환자 ID 목록 (쉼표 구분, 예: patient_001,patient_003). 미지정 시 전체"
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    start    = _parse_date(args.start)
    end      = _parse_date(args.end)

    if not data_dir.exists():
        log.error(f"폴더를 찾을 수 없습니다: {data_dir}")
        sys.exit(1)

    db_url = os.getenv("DATABASE_URL", DEFAULT_DB_URL)
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    db = Session()

    log.info("=" * 60)
    log.info(f"데이터 폴더 : {data_dir}")
    log.info(f"날짜 범위   : {start} ~ {end}")
    log.info(f"DB          : {db_url}")
    log.info("=" * 60)

    # ── patients_index.json 읽기 ─────────────────────────────────────────────
    index_path = data_dir / "patients_index.json"
    if not index_path.exists():
        log.error("patients_index.json 없음. --data-dir 경로가 맞는지 확인하세요.")
        sys.exit(1)

    with open(index_path, encoding="utf-8") as f:
        index_data = json.load(f)
    patients_index = index_data.get("patients", [])
    log.info(f"환자 수: {len(patients_index)}명")

    # ── 환자 계정 생성/확인 ───────────────────────────────────────────────────
    log.info("\n[1/3] 환자 계정 확인/생성")
    patient_map = ensure_patients(db, patients_index, args.password, data_dir)

    # ── 기존 기록 초기화 ──────────────────────────────────────────────────────
    if args.clear:
        log.info("\n[초기화] 기존 더미 기록 삭제 중...")
        clear_dummy_records(db, patient_map)

    # ── 포맷 자동 감지 및 임포트 ─────────────────────────────────────────────
    has_csv = (data_dir / "daily_records.csv").exists()

    only_patients = set(args.patients.split(",")) if args.patients else None
    if only_patients:
        log.info(f"환자 필터: {', '.join(sorted(only_patients))}")

    if has_csv:
        log.info("\n[2/3] CSV 모드로 임포트")
        count = import_from_csv(db, data_dir, patient_map, start, end, only_patients)
    else:
        log.info("\n[2/3] JSON 모드로 임포트")
        count = import_from_json(db, data_dir, patient_map, start, end)

    # ── 결과 요약 ─────────────────────────────────────────────────────────────
    log.info("\n[3/3] 결과 요약")
    log.info("=" * 60)
    log.info(f"임포트된 일일 기록: {count}건")
    log.info(f"생성된/확인된 환자 계정: {len(patient_map)}명")
    log.info("")
    log.info("테스트 계정 (비밀번호: {})".format(args.password))
    for pid, uid in patient_map.items():
        num = pid.replace("patient_", "").zfill(8)
        log.info(f"  010{num}  (id={uid})")
    log.info("=" * 60)

    db.close()


if __name__ == "__main__":
    main()
