"""
DB 마이그레이션 스크립트: users.email → users.phone_number

기존 DB가 있을 때 실행하세요.
새로운 DB에서 시작하는 경우 main.py의 Base.metadata.create_all()이 자동으로 처리합니다.

실행 방법:
    $env:DATABASE_URL = "postgresql://capd_user:capd_pass@localhost:5432/capd"
    python backend/scripts/migrate_email_to_phone.py
"""
import os
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://capd_user:capd_pass@localhost:5432/capd")
engine = create_engine(DATABASE_URL)


def migrate():
    with engine.connect() as conn:
        # 1. user_role_enum이 없으면 생성
        conn.execute(text("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_role_enum') THEN
                    CREATE TYPE user_role_enum AS ENUM ('patient', 'doctor', 'admin');
                END IF;
            END$$;
        """))

        # 2. registration_status_enum 생성
        conn.execute(text("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'registration_status_enum') THEN
                    CREATE TYPE registration_status_enum AS ENUM ('pending', 'approved', 'rejected', 'completed');
                END IF;
            END$$;
        """))

        # 3. users 테이블: email → phone_number 컬럼 변경
        # 컬럼이 이미 phone_number이면 스킵
        col_check = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='users' AND column_name='phone_number'
        """)).fetchone()

        if not col_check:
            print("[migrate] users.email → phone_number 변경 중...")
            # email 컬럼이 있으면 rename
            email_check = conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='users' AND column_name='email'
            """)).fetchone()
            if email_check:
                conn.execute(text("ALTER TABLE users RENAME COLUMN email TO phone_number"))
                conn.execute(text("ALTER TABLE users ALTER COLUMN phone_number TYPE VARCHAR(20)"))
            else:
                conn.execute(text("ALTER TABLE users ADD COLUMN phone_number VARCHAR(20)"))
            print("[migrate] users.phone_number 컬럼 추가 완료")
        else:
            print("[migrate] users.phone_number 컬럼 이미 존재")

        # 4. birth_date 컬럼 추가 (없으면)
        birth_check = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='users' AND column_name='birth_date'
        """)).fetchone()
        if not birth_check:
            conn.execute(text("ALTER TABLE users ADD COLUMN birth_date VARCHAR(10)"))
            print("[migrate] users.birth_date 컬럼 추가 완료")

        # 5. hospitals 테이블
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS hospitals (
                id BIGSERIAL PRIMARY KEY,
                name VARCHAR(200) UNIQUE NOT NULL,
                address VARCHAR(500),
                created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
            )
        """))
        print("[migrate] hospitals 테이블 확인 완료")

        # 6. doctor_licenses 테이블
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS doctor_licenses (
                id BIGSERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                birth_date VARCHAR(10) NOT NULL,
                license_number VARCHAR(50) UNIQUE NOT NULL,
                hospital_id BIGINT REFERENCES hospitals(id) ON DELETE SET NULL,
                is_registered BOOLEAN DEFAULT FALSE NOT NULL
            )
        """))
        print("[migrate] doctor_licenses 테이블 확인 완료")

        # 7. doctor_profiles 테이블
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS doctor_profiles (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                birth_date VARCHAR(10) NOT NULL,
                license_number VARCHAR(50) NOT NULL,
                hospital_id BIGINT REFERENCES hospitals(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
            )
        """))
        print("[migrate] doctor_profiles 테이블 확인 완료")

        # 8. patient_registrations 테이블
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS patient_registrations (
                id BIGSERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                birth_date VARCHAR(10) NOT NULL,
                hospital_id BIGINT REFERENCES hospitals(id) ON DELETE SET NULL,
                doctor_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                status registration_status_enum DEFAULT 'pending' NOT NULL,
                reject_reason TEXT,
                user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
            )
        """))
        print("[migrate] patient_registrations 테이블 확인 완료")

        conn.commit()
        print("[migrate] ✅ 마이그레이션 완료!")


if __name__ == "__main__":
    migrate()
