"""
migrate_add_analytics_tables.py
================================
AB180 전환설계(AB180_전환설계.md 9-1) — 분석 리포트용 집계 테이블 2종 신설

Silver: patient_daily_metrics   — ai/tools/data_engineering.py build_daily_model_row() 출력 적재본
Gold:   patient_daily_analytics — ai/tools/analytics.py run_all_tasks() 출력 적재본

둘 다 (patient_id, record_date) 단위로 하루 1행. 온디맨드(backend) 즉석 계산 결과를
그대로 적재하는 캐시/롤업 테이블이며, 나중에 Airflow 배치(daily_metrics_rollup DAG)가
전체 환자를 매일 채워 넣는 대상 테이블이기도 하다.

실행 (로컬 docker-compose 환경):
  docker compose -f docker-compose.prod.yml exec backend python scripts/migrate_add_analytics_tables.py

실행 (GCP, Supabase 대상 — Cloud Shell):
  DATABASE_URL 환경변수로 Supabase 연결 문자열을 주입한 뒤 그대로 실행.
  (아래 채팅 안내의 Cloud Shell 명령 블록 참고 — 서비스명 capd-backend 기준)

멱등: 여러 번 실행해도 안전 (IF NOT EXISTS 전부 사용).
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
        print("=== [1/4] patient_daily_metrics (Silver) 테이블 생성 ===")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS patient_daily_metrics (
                id                      BIGSERIAL PRIMARY KEY,
                patient_id              BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                record_date             DATE NOT NULL,

                -- Exchange Aggregate (data_engineering._aggregate_exchanges)
                exchange_count          SMALLINT,
                missing_exchange_slots  SMALLINT,
                drain_sum_g             NUMERIC(8,1),
                infused_sum_g           NUMERIC(8,1),
                recorded_uf_sum_g       NUMERIC(8,1),
                calculated_uf_sum_g     NUMERIC(8,1),
                uf_min_g                NUMERIC(8,1),
                uf_std_g                NUMERIC(8,2),
                dwell_mean_minutes      NUMERIC(6,1),
                dwell_std_minutes       NUMERIC(6,1),
                concentration_max       NUMERIC(4,2),

                -- UF 검증 (build_daily_model_row)
                reported_total_uf_g     NUMERIC(8,1),
                uf_discrepancy_g        NUMERIC(8,1),

                -- Daily 기본 지표
                body_weight_kg          NUMERIC(5,1),
                fasting_blood_sugar     NUMERIC(6,1),
                urination_count         SMALLINT,
                cloudy_dialysate        SMALLINT,

                -- 혈압 파생 (data_engineering._parse_bp)
                systolic_bp             SMALLINT,
                diastolic_bp            SMALLINT,
                pulse_pressure          SMALLINT,
                mean_arterial_pressure  NUMERIC(5,1),

                -- 자유 메모 (수치 분석 제외, LLM 컨텍스트용)
                note                    TEXT,

                created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                CONSTRAINT uq_patient_daily_metrics UNIQUE (patient_id, record_date)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_pdm_patient_date
            ON patient_daily_metrics (patient_id, record_date DESC);
        """)
        print("  → 완료")

        print("=== [2/4] patient_daily_analytics (Gold) 테이블 생성 ===")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS patient_daily_analytics (
                id                  BIGSERIAL PRIMARY KEY,
                patient_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                record_date         DATE NOT NULL,

                -- analytics.run_all_tasks() 4개 Task 결과 그대로 적재
                trend_json          JSONB,   -- task1_trend_analysis
                anomaly_json        JSONB,   -- task2_anomaly_detection
                correlation_json    JSONB,   -- task3_attribute_correlation (Spearman)
                eda_json            JSONB,   -- task4_eda

                has_anomaly         BOOLEAN NOT NULL DEFAULT FALSE,
                anomaly_attrs       TEXT[],  -- 이상탐지된 지표명 배열

                computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                CONSTRAINT uq_patient_daily_analytics UNIQUE (patient_id, record_date)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_pda_patient_date
            ON patient_daily_analytics (patient_id, record_date DESC);
        """)
        print("  → 완료")

        print("=== [3/4] 이상치 선제 스캔용 부분 인덱스 (Airflow 배치가 조회) ===")
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_pda_has_anomaly
            ON patient_daily_analytics (record_date)
            WHERE has_anomaly = TRUE;
        """)
        print("  → 완료")

        print("=== [4/4] 검증 ===")
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_name IN ('patient_daily_metrics', 'patient_daily_analytics')
            ORDER BY table_name;
        """)
        found = [r[0] for r in cur.fetchall()]
        print(f"  생성 확인된 테이블: {found}")
        if len(found) != 2:
            raise RuntimeError(f"테이블 생성 검증 실패 — found={found}")

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
