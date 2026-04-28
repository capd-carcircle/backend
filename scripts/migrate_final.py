"""
통합 마이그레이션 스크립트 — migrate_final.py
==============================================

이전 미실행 마이그레이션 + 신규 변경 사항을 한 번에 적용.

적용 내용:
  1. common_questions.question_type / options 컬럼 추가       (이전 미실행)
  2. common_questions.target_all_patients 컬럼 추가           (이전 미실행)
  3. question_patient_assignments 테이블 생성                  (이전 미실행)
  4. users.hospital_id 컬럼 추가 (환자 전용, FK→hospitals)     (신규)
  5. users.self_memo 컬럼 추가 (환자 자기 메모)                (신규)
  6. patient_notes 테이블 생성 (의사→환자 단일 메모)           (신규)
  7. patient_registrations → users.hospital_id 데이터 복사    (신규)

멱등성: 모든 단계가 IF NOT EXISTS / 컬럼 존재 확인 후 수행.

서버 실행:
    cd ~/capd
    docker compose -f docker-compose.prod.yml exec backend python scripts/migrate_final.py
"""

import os
import sys
import psycopg2

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://capd_user:capd_pass@db:5432/capd"
)


def run():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        # ──────────────────────────────────────────────────────────
        # 1. ai_question_type_enum 확보
        # ──────────────────────────────────────────────────────────
        cur.execute("SELECT 1 FROM pg_type WHERE typname = 'ai_question_type_enum'")
        if cur.fetchone():
            print("✅ [1] ai_question_type_enum 이미 존재")
        else:
            cur.execute("""
                CREATE TYPE ai_question_type_enum
                AS ENUM ('yes_no', 'single_select', 'multi_select', 'short_text');
            """)
            print("✅ [1] ai_question_type_enum 생성")

        # ──────────────────────────────────────────────────────────
        # 2. common_questions.question_type
        # ──────────────────────────────────────────────────────────
        cur.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name='common_questions' AND column_name='question_type'
        """)
        if cur.fetchone():
            print("✅ [2] common_questions.question_type 이미 존재")
        else:
            cur.execute("""
                ALTER TABLE common_questions
                ADD COLUMN question_type ai_question_type_enum NOT NULL DEFAULT 'yes_no';
            """)
            print("✅ [2] common_questions.question_type 추가")

        # ──────────────────────────────────────────────────────────
        # 3. common_questions.options
        # ──────────────────────────────────────────────────────────
        cur.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name='common_questions' AND column_name='options'
        """)
        if cur.fetchone():
            print("✅ [3] common_questions.options 이미 존재")
        else:
            cur.execute("ALTER TABLE common_questions ADD COLUMN options TEXT NULL;")
            print("✅ [3] common_questions.options 추가")

        # ──────────────────────────────────────────────────────────
        # 4. common_questions.target_all_patients
        # ──────────────────────────────────────────────────────────
        cur.execute("""
            ALTER TABLE common_questions
            ADD COLUMN IF NOT EXISTS target_all_patients BOOLEAN NOT NULL DEFAULT TRUE;
        """)
        print("✅ [4] common_questions.target_all_patients 추가 (또는 이미 존재)")

        # ──────────────────────────────────────────────────────────
        # 5. question_patient_assignments 테이블
        # ──────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS question_patient_assignments (
                id          BIGSERIAL PRIMARY KEY,
                question_id BIGINT NOT NULL
                                REFERENCES common_questions(id) ON DELETE CASCADE,
                patient_id  BIGINT NOT NULL
                                REFERENCES users(id) ON DELETE CASCADE,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_qpa_question_patient UNIQUE (question_id, patient_id)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS ix_qpa_question_id
                ON question_patient_assignments (question_id);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS ix_qpa_patient_id
                ON question_patient_assignments (patient_id);
        """)
        print("✅ [5] question_patient_assignments 테이블 및 인덱스")

        # ──────────────────────────────────────────────────────────
        # 6. users.hospital_id (환자 전용, FK→hospitals)
        # ──────────────────────────────────────────────────────────
        cur.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name='users' AND column_name='hospital_id'
        """)
        if cur.fetchone():
            print("✅ [6] users.hospital_id 이미 존재")
        else:
            cur.execute("""
                ALTER TABLE users
                ADD COLUMN hospital_id BIGINT NULL
                    REFERENCES hospitals(id) ON DELETE SET NULL;
            """)
            print("✅ [6] users.hospital_id 추가")

        # ──────────────────────────────────────────────────────────
        # 7. users.self_memo (환자 자기 메모)
        # ──────────────────────────────────────────────────────────
        cur.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name='users' AND column_name='self_memo'
        """)
        if cur.fetchone():
            print("✅ [7] users.self_memo 이미 존재")
        else:
            cur.execute("ALTER TABLE users ADD COLUMN self_memo TEXT NULL;")
            print("✅ [7] users.self_memo 추가")

        # ──────────────────────────────────────────────────────────
        # 8. patient_notes 테이블 (의사→환자 단일 메모)
        # ──────────────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS patient_notes (
                id          BIGSERIAL PRIMARY KEY,
                doctor_id   BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                patient_id  BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                content     TEXT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_patient_notes_doctor_patient UNIQUE (doctor_id, patient_id)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS ix_patient_notes_doctor_id
                ON patient_notes (doctor_id);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS ix_patient_notes_patient_id
                ON patient_notes (patient_id);
        """)
        print("✅ [8] patient_notes 테이블 및 인덱스")

        # ──────────────────────────────────────────────────────────
        # 9. patient_registrations → users.hospital_id 데이터 복사
        #    - completed 상태인 레코드 기준, 가장 최근 것 우선
        #    - users.hospital_id가 NULL인 환자만 업데이트
        # ──────────────────────────────────────────────────────────
        cur.execute("""
            UPDATE users u
            SET hospital_id = pr.hospital_id
            FROM (
                SELECT DISTINCT ON (user_id)
                    user_id, hospital_id
                FROM patient_registrations
                WHERE status = 'completed'
                  AND user_id IS NOT NULL
                  AND hospital_id IS NOT NULL
                ORDER BY user_id, updated_at DESC
            ) pr
            WHERE u.id = pr.user_id
              AND u.hospital_id IS NULL;
        """)
        cur.execute("SELECT COUNT(*) FROM users WHERE hospital_id IS NOT NULL AND role='patient'")
        count = cur.fetchone()[0]
        print(f"✅ [9] users.hospital_id 복사 완료 — 현재 병원 정보 있는 환자: {count}명")

        conn.commit()
        print("\n🎉 migrate_final.py 전체 완료")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ 오류 발생 (롤백): {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    run()
