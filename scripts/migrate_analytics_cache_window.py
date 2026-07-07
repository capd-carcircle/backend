"""
migrate_analytics_cache_window.py
==================================
patient_daily_analytics(Gold) 캐시를 window별로 분리 저장하도록 스키마 변경.

문제: 지금은 (patient_id, record_date) 딱 1행에 가장 최근 계산된 window 하나만
저장돼 있어서, 분석 리포트에서 7/30/90일 버튼을 오갈 때마다 직전 캐시와 window가
달라 서로 무효화시키고 매번 재계산됨(CLAUDE.md "알려진 제한사항" 참고).

해결: window_days 컬럼을 추가하고 UNIQUE 제약을 (patient_id, record_date)에서
(patient_id, record_date, window_days)로 확장 — 이제 같은 날짜라도 window별로
별도 행에 캐시되므로 7/30/90 전환해도 서로 안 지움(trend/anomaly/eda는 원래
window와 무관한 값이라 3개 행에 걸쳐 약간 중복 저장되지만, 캐시 테이블이라
저장 비용은 무시 가능한 수준).

기존 캐시 행의 window_days는 correlation_json->>'window_days'에서 백필
(task3_attribute_correlation이 두 분기 모두 이 값을 항상 채워두므로 100% 백필
가능 — backend/app/services/analytics_engine.py 확인함).

실행 (로컬 docker-compose 환경):
  docker compose -f docker-compose.prod.yml exec backend python scripts/migrate_analytics_cache_window.py

실행 (GCP, Supabase 대상 — Cloud Shell):
  DATABASE_URL 환경변수로 Supabase 연결 문자열을 주입한 뒤 그대로 실행.
  (아래 채팅 안내의 Cloud Shell 명령 블록 참고 — 서비스명 capd-backend 기준)

멱등: 여러 번 실행해도 안전(컬럼/제약 존재 여부 확인 후 처리, ADD COLUMN IF NOT EXISTS 등).
"""
import os
import sys

import psycopg2

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://capd_user:capd_pass@db:5432/capd",
)


def run():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        print("=== [1/5] window_days 컬럼 추가 ===")
        cur.execute("""
            ALTER TABLE patient_daily_analytics
            ADD COLUMN IF NOT EXISTS window_days INTEGER;
        """)
        print("  → 완료")

        print("=== [2/5] 기존 캐시 행 백필 (correlation_json->>'window_days') ===")
        cur.execute("""
            UPDATE patient_daily_analytics
            SET window_days = (correlation_json->>'window_days')::int
            WHERE window_days IS NULL AND correlation_json IS NOT NULL;
        """)
        print(f"  → {cur.rowcount}행 백필")

        print("=== [3/5] 백필 안 된 행 정리 (순수 캐시라 유실 위험 없음, 다음 조회 시 자동 재계산) ===")
        cur.execute("DELETE FROM patient_daily_analytics WHERE window_days IS NULL;")
        print(f"  → {cur.rowcount}행 삭제")

        print("=== [4/5] NOT NULL 제약 + UNIQUE 키 확장 ===")
        cur.execute("""
            ALTER TABLE patient_daily_analytics
            ALTER COLUMN window_days SET NOT NULL;
        """)
        cur.execute("""
            ALTER TABLE patient_daily_analytics
            DROP CONSTRAINT IF EXISTS uq_patient_daily_analytics;
        """)
        cur.execute("""
            ALTER TABLE patient_daily_analytics
            ADD CONSTRAINT uq_patient_daily_analytics
            UNIQUE (patient_id, record_date, window_days);
        """)
        print("  → 완료")

        print("=== [5/5] 검증 ===")
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'patient_daily_analytics' AND column_name = 'window_days';
        """)
        if not cur.fetchone():
            raise RuntimeError("window_days 컬럼 생성 검증 실패")

        cur.execute("""
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'patient_daily_analytics'::regclass
              AND conname = 'uq_patient_daily_analytics';
        """)
        con = cur.fetchone()
        if not con:
            raise RuntimeError("UNIQUE 제약 재생성 검증 실패")
        print(f"  window_days 컬럼: 확인됨 / UNIQUE 제약({con[0]}): (patient_id, record_date, window_days) 확인됨")

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
