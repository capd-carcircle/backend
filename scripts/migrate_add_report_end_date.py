"""
patient_notes 테이블에 last_report_end_date 컬럼 추가
실행: docker compose -f docker-compose.prod.yml exec backend python scripts/migrate_add_report_end_date.py
"""
import os
import psycopg2

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://capd_user:capd_pass@db:5432/capd",
)

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

cur.execute("""
    ALTER TABLE patient_notes
    ADD COLUMN IF NOT EXISTS last_report_end_date DATE;
""")

conn.commit()
cur.close()
conn.close()

print("✅ patient_notes.last_report_end_date 컬럼 추가 완료")
