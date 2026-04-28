"""
migrate_assignment_system.py
============================
의사-환자 담당 히스토리 시스템 마이그레이션

변경사항:
1. patient_doctor_assignments 테이블 생성
2. patient_registrations에 request_type 컬럼 추가 ('connect' | 'discharge')
3. patient_registrations.doctor_id 를 nullable로 변경
4. 기존 users.doctor_id 데이터 → patient_doctor_assignments 백필

실행:
  docker compose -f docker-compose.prod.yml exec backend python scripts/migrate_assignment_system.py
"""
import os
import sys

import psycopg2
from psycopg2.extras import execute_values

DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://capd_user:capd_pass@db:5432/capd",
)


def run():
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        print("=== [1/4] patient_doctor_assignments 테이블 생성 ===")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS patient_doctor_assignments (
                id          BIGSERIAL PRIMARY KEY,
                patient_id  BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                doctor_id   BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                ended_at    TIMESTAMPTZ,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

        # 부분 유니크 인덱스: 환자당 현재 담당(ended_at IS NULL)은 1명만
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_patient_current_doctor
            ON patient_doctor_assignments (patient_id)
            WHERE ended_at IS NULL;
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_pda_doctor_id
            ON patient_doctor_assignments (doctor_id);
        """)

        print("  → 완료")

        print("=== [2/4] patient_registrations에 request_type 컬럼 추가 ===")
        cur.execute("""
            ALTER TABLE patient_registrations
            ADD COLUMN IF NOT EXISTS request_type VARCHAR(20) NOT NULL DEFAULT 'connect';
        """)
        print("  → 완료")

        print("=== [3/4] patient_registrations.doctor_id nullable 처리 ===")
        cur.execute("""
            ALTER TABLE patient_registrations
            ALTER COLUMN doctor_id DROP NOT NULL;
        """)
        print("  → 완료")

        print("=== [4/4] users.doctor_id 데이터 → patient_doctor_assignments 백필 ===")
        cur.execute("""
            SELECT id, doctor_id, created_at
            FROM users
            WHERE role = 'doctor' IS FALSE
              AND doctor_id IS NOT NULL
        """)
        # role이 'patient'인 사용자 + doctor_id 있는 경우
        cur.execute("""
            SELECT u.id AS patient_id, u.doctor_id, u.created_at
            FROM users u
            WHERE u.role = 'patient'
              AND u.doctor_id IS NOT NULL
        """)
        rows = cur.fetchall()
        print(f"  백필 대상 환자 수: {len(rows)}")

        if rows:
            # 이미 assignments가 있는 환자는 스킵
            cur.execute("SELECT patient_id FROM patient_doctor_assignments")
            existing = {r[0] for r in cur.fetchall()}

            insert_rows = [
                (patient_id, doctor_id, started_at)
                for patient_id, doctor_id, started_at in rows
                if patient_id not in existing
            ]
            if insert_rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO patient_doctor_assignments (patient_id, doctor_id, started_at)
                    VALUES %s
                    """,
                    insert_rows,
                )
                print(f"  → {len(insert_rows)}건 백필 완료")
            else:
                print("  → 백필 스킵 (이미 데이터 존재)")

        conn.commit()
        print("\n✅ 마이그레이션 완료")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ 오류 발생: {e}")
        sys.exit(1)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    run()
