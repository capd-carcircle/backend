"""
기존 데이터 → 새 구조 마이그레이션 스크립트

수행 내용:
1. 세브란스병원 확보 (없으면 생성)
2. 테스트 의사(01011112222)에 DoctorProfile 생성 → 세브란스병원
3. 모든 기존 환자(테스트 환자 + 더미 환자)를
   patient_registrations(status=completed)으로 테스트 의사에 연결

실행 방법:
    $env:DATABASE_URL = "postgresql://capd_user:capd_pass@localhost:5432/capd"
    python backend/scripts/seed_severance_data.py
"""
import os
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://capd_user:capd_pass@localhost:5432/capd")
engine = create_engine(DATABASE_URL)


def run():
    with engine.connect() as conn:

        # ── 1. 세브란스병원 확보 ─────────────────────────────────
        hospital = conn.execute(text(
            "SELECT id FROM hospitals WHERE name = '세브란스병원'"
        )).fetchone()

        if hospital:
            hospital_id = hospital[0]
            print(f"[seed] 세브란스병원 기존 사용 (id={hospital_id})")
        else:
            hospital_id = conn.execute(text("""
                INSERT INTO hospitals (name, address, created_at)
                VALUES ('세브란스병원', '서울특별시 서대문구 연세로 50-1', NOW())
                RETURNING id
            """)).fetchone()[0]
            print(f"[seed] 세브란스병원 신규 생성 (id={hospital_id})")

        # ── 2. 테스트 의사 확보 ──────────────────────────────────
        doctor = conn.execute(text(
            "SELECT id FROM users WHERE phone_number = '01011112222' AND role = 'doctor'"
        )).fetchone()

        if not doctor:
            print("[seed] ❌ 테스트 의사(01011112222)가 없습니다. 서버를 한 번 실행해 시드 데이터를 생성하세요.")
            return

        doctor_id = doctor[0]
        print(f"[seed] 테스트 의사 확인 (id={doctor_id})")

        # ── 3. DoctorProfile 생성 (없으면) ───────────────────────
        profile = conn.execute(text(
            "SELECT id FROM doctor_profiles WHERE user_id = :uid"
        ), {"uid": doctor_id}).fetchone()

        if profile:
            print("[seed] DoctorProfile 이미 존재 — 병원 업데이트")
            conn.execute(text(
                "UPDATE doctor_profiles SET hospital_id = :hid WHERE user_id = :uid"
            ), {"hid": hospital_id, "uid": doctor_id})
        else:
            conn.execute(text("""
                INSERT INTO doctor_profiles (user_id, birth_date, license_number, hospital_id, created_at)
                VALUES (:uid, '1980-01-15', 'NEPH-2024-001', :hid, NOW())
            """), {"uid": doctor_id, "hid": hospital_id})
            print("[seed] DoctorProfile 생성 완료 (세브란스병원)")

        # ── 4. 기존 환자 전체 조회 ───────────────────────────────
        patients = conn.execute(text(
            "SELECT id, name FROM users WHERE role = 'patient' AND is_active = true"
        )).fetchall()
        print(f"[seed] 기존 환자 {len(patients)}명 발견")

        # ── 5. 환자마다 patient_registrations 연결 ───────────────
        linked = 0
        skipped = 0
        for patient_id, patient_name in patients:
            # 이미 이 의사와 연결된 completed 레코드가 있으면 스킵
            existing = conn.execute(text("""
                SELECT id FROM patient_registrations
                WHERE user_id = :pid AND doctor_id = :did AND status = 'completed'
            """), {"pid": patient_id, "did": doctor_id}).fetchone()

            if existing:
                skipped += 1
                continue

            conn.execute(text("""
                INSERT INTO patient_registrations
                    (name, birth_date, hospital_id, doctor_id, status, user_id, created_at, updated_at)
                VALUES
                    (:name, '2000-01-01', :hid, :did, 'completed', :pid, NOW(), NOW())
            """), {
                "name": patient_name,
                "hid": hospital_id,
                "did": doctor_id,
                "pid": patient_id,
            })
            linked += 1

        conn.commit()
        print(f"[seed] 연결 완료: {linked}명 신규 / {skipped}명 이미 연결됨")
        print("[seed] ✅ 완료! 테스트 의사(01011112222)의 환자 목록에서 모든 환자를 볼 수 있습니다.")


if __name__ == "__main__":
    run()
