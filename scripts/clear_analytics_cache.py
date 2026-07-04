"""
clear_analytics_cache.py
=========================
분석 리포트 Gold 캐시(patient_daily_analytics) 전체 삭제.

배경: 제수량(UF) 중복 지표 통합(reported_total_uf_g·recorded_uf_sum_g 제거,
calculated_uf_sum_g만 남김) 이후에도, 통합 전에 이미 계산돼 캐시된 리포트는
trend_json/anomaly_json/correlation_json 안에 예전 3종 지표가 그대로 남아있음.
캐시 재사용 로직(_read_cache)은 window_days 일치 여부만 확인하고 어떤 지표가
들었는지는 안 보기 때문에, 캐시가 있는 환자는 리포트를 다시 열어도 자동으로는
정리되지 않음 — 그래서 한 번 비워서 강제 재계산되게 함.

patient_daily_metrics(Silver)는 원본 계산값 컬럼이라 안전하게 그대로 둠
(recorded_uf_sum_g 등 컬럼 자체는 여전히 유효한 값 — 리포트가 그 중 무엇을
분석/표시할지만 바뀐 것이라 Silver는 지울 필요 없음).

Gold는 순수 캐시 테이블이라 비워도 데이터 유실 없음 — 의사가 해당 환자
분석 리포트를 다음에 열 때 온디맨드로 재계산돼 다시 채워짐.

실행 (로컬 docker-compose 환경):
  docker compose -f docker-compose.prod.yml exec backend python scripts/clear_analytics_cache.py

실행 (GCP, Supabase 대상 — Cloud Shell):
  DATABASE_URL 환경변수로 Supabase 연결 문자열을 주입한 뒤 그대로 실행.
  (아래 채팅 안내의 Cloud Shell 명령 블록 참고 — 서비스명 capd-backend 기준)

멱등: 여러 번 실행해도 안전 (없으면 0행 삭제로 끝).
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
        print("=== [1/2] 삭제 전 patient_daily_analytics(Gold) 행 수 확인 ===")
        cur.execute("SELECT COUNT(*) FROM patient_daily_analytics;")
        before = cur.fetchone()[0]
        print(f"  삭제 대상: {before}행")

        print("=== [2/2] 전체 삭제 ===")
        cur.execute("DELETE FROM patient_daily_analytics;")
        cur.execute("SELECT COUNT(*) FROM patient_daily_analytics;")
        after = cur.fetchone()[0]
        print(f"  삭제 후 남은 행: {after}행")
        if after != 0:
            raise RuntimeError(f"삭제 검증 실패 — 남은 행 {after}")

        conn.commit()
        print(f"\n✅ 캐시 정리 완료 ({before}행 삭제됨) — 다음 리포트 조회부터 자동 재계산")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ 오류 발생: {e}")
        sys.exit(1)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    run()
