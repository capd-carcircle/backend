"""
마이그레이션: record_status_enum에 'draft' 값 추가

실행 방법 (로컬):
    $env:DATABASE_URL = "postgresql://capd_user:capd_pass@localhost:5432/capd"
    python backend/scripts/migrate_add_draft_status.py

이미 draft 값이 있으면 아무것도 하지 않음 (멱등성).
"""

import os
import sys

import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://capd_user:capd_pass@localhost:5432/capd")


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()

    # 현재 enum 값 확인
    cur.execute("""
        SELECT enumlabel
        FROM pg_enum
        JOIN pg_type ON pg_type.oid = pg_enum.enumtypid
        WHERE pg_type.typname = 'record_status_enum'
    """)
    existing = {row[0] for row in cur.fetchall()}
    print(f"현재 enum 값: {existing}")

    if "draft" in existing:
        print("✅ 'draft' 값이 이미 존재합니다. 마이그레이션 불필요.")
        cur.close()
        conn.close()
        return

    # draft를 submitted 앞에 추가 (PostgreSQL 10+에서 BEFORE 지원)
    # PostgreSQL 10 미만이면 단순 ADD VALUE 사용
    try:
        cur.execute("ALTER TYPE record_status_enum ADD VALUE 'draft' BEFORE 'submitted';")
        print("✅ 'draft' 값을 'submitted' 앞에 추가했습니다.")
    except psycopg2.errors.InvalidParameterValue:
        # BEFORE 미지원 버전
        conn.rollback()
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("ALTER TYPE record_status_enum ADD VALUE 'draft';")
        print("✅ 'draft' 값을 추가했습니다.")

    cur.close()
    conn.close()
    print("마이그레이션 완료.")


if __name__ == "__main__":
    main()
