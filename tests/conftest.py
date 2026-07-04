"""
conftest.py — backend pytest 공용 fixture

DB: 진짜 Postgres(+pgvector) 테스트 DB를 씀(SQLite 아님) — DocumentChunk 모델이
pgvector Vector 타입을 쓰기 때문에 SQLite로는 스키마 자체가 안 만들어짐.

격리: 테스트마다 커넥션 하나를 열고 트랜잭션을 시작 -> 그 안에 SAVEPOINT(nested
transaction)로 세션을 붙여서, 라우트 핸들러 안의 db.commit()은 SAVEPOINT만
커밋하고 바깥 트랜잭션은 안 끝나게 한다. 테스트가 끝나면 바깥 트랜잭션을 통째로
롤백해서 그 테스트가 만든 데이터를 전부 취소한다(FastAPI 공식 문서의 표준 테스트
격리 recipe).

로컬 실행: docker-compose.yml의 db 서비스(pgvector/pgvector:pg16)에 테스트 전용
DB를 하나 더 만들어서 TEST_DATABASE_URL로 지정.
  createdb -h localhost -U capd_user capd_test
  TEST_DATABASE_URL=postgresql://capd_user:capd_pass@localhost:5432/capd_test pytest tests

CI: deploy.yml의 ci job에 pgvector/pgvector:pg16 service container를 추가해 사용.

테스트 Postgres에 연결이 안 되면(로컬에서 DB 안 띄운 채로 test_ai_parity.py만
돌리고 싶은 경우 등) DB 관련 fixture만 skip 처리하고, DB 불필요한
test_ai_parity.py는 영향받지 않는다.
"""
import os
import random
import sys

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://capd_user:capd_pass@localhost:5432/capd_test",
    ),
)
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest-only")
os.environ.setdefault("ENVIRONMENT", "test")  # "production"만 아니면 됨(main.py 시드 조건)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.auth import create_access_token, hash_password
from app.core.database import Base, get_db
from app.models.hospital import DoctorProfile, Hospital
from app.models.patient_assignment import PatientDoctorAssignment
from app.models.user import User, UserRole

TEST_DATABASE_URL = os.environ["DATABASE_URL"]
_engine = create_engine(TEST_DATABASE_URL)

# patient_daily_metrics(Silver)/patient_daily_analytics(Gold)는 ORM 모델이 없고
# raw SQL(scripts/migrate_add_analytics_tables.py)로만 생성되는 테이블이라
# Base.metadata.create_all()로는 안 만들어짐 -- 테스트 DB에도 동일하게 직접 만들어야 함.
# ⚠️ 저 마이그레이션 스크립트가 바뀌면 이 SQL도 같이 맞춰줄 것.
_ANALYTICS_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS patient_daily_metrics (
        id                      BIGSERIAL PRIMARY KEY,
        patient_id              BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        record_date             DATE NOT NULL,
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
        reported_total_uf_g     NUMERIC(8,1),
        uf_discrepancy_g        NUMERIC(8,1),
        body_weight_kg          NUMERIC(5,1),
        fasting_blood_sugar     NUMERIC(6,1),
        urination_count         SMALLINT,
        cloudy_dialysate        SMALLINT,
        systolic_bp             SMALLINT,
        diastolic_bp            SMALLINT,
        pulse_pressure          SMALLINT,
        mean_arterial_pressure  NUMERIC(5,1),
        note                    TEXT,
        created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_patient_daily_metrics UNIQUE (patient_id, record_date)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS patient_daily_analytics (
        id                  BIGSERIAL PRIMARY KEY,
        patient_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        record_date         DATE NOT NULL,
        trend_json          JSONB,
        anomaly_json        JSONB,
        correlation_json    JSONB,
        eda_json            JSONB,
        has_anomaly         BOOLEAN NOT NULL DEFAULT FALSE,
        anomaly_attrs       TEXT[],
        computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_patient_daily_analytics UNIQUE (patient_id, record_date)
    );
    """,
]


@pytest.fixture(scope="session")
def _pgvector_ready():
    """
    pgvector 확장만 미리 생성. Base.metadata.create_all()이 document_chunks
    (Vector 컬럼)를 만들 때 필요 -- 반드시 create_all보다 먼저 실행돼야 함.
    Postgres 연결 안 되면 skip.
    """
    try:
        with _engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
    except OperationalError as e:
        pytest.skip(f"테스트 Postgres에 연결할 수 없음 ({TEST_DATABASE_URL}): {e}")


@pytest.fixture(scope="session")
def test_client(_pgvector_ready):
    """
    세션당 1회만 FastAPI lifespan 실행(Base.metadata.create_all + 개발 시드).
    이후 각 테스트는 dependency override로 격리된 db_session을 주입받는다.
    """
    from main import app  # backend/main.py의 FastAPI 인스턴스 (sys.path에 backend root 추가돼 있음)

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def _analytics_tables_ready(test_client):
    """
    patient_daily_metrics/patient_daily_analytics(raw SQL, FK -> users.id) 생성.
    ⚠️ test_client 다음에 실행돼야 함 -- Base.metadata.create_all()이 먼저 users
    테이블을 만들어야 이 FK 참조가 성립함(반대 순서면 UndefinedTable 에러).
    """
    with _engine.connect() as conn:
        for stmt in _ANALYTICS_TABLES_SQL:
            conn.execute(text(stmt))
        conn.commit()


@pytest.fixture()
def db_session(_analytics_tables_ready):
    """테스트 1개당 트랜잭션+SAVEPOINT로 격리된 세션. 끝나면 전부 롤백."""
    connection = _engine.connect()
    outer_trans = connection.begin()
    TestSession = sessionmaker(bind=connection)
    session = TestSession()

    nested = connection.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(sess, transaction):
        nonlocal nested
        if not nested.is_active:
            nested = connection.begin_nested()

    try:
        yield session
    finally:
        session.close()
        outer_trans.rollback()
        connection.close()


@pytest.fixture()
def client(test_client, db_session):
    """db_session으로 get_db 의존성을 오버라이드한 TestClient."""
    from main import app

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


# ── 공용 엔티티 fixture ──────────────────────────────────────────

def _rand_phone() -> str:
    return f"010{random.randint(10_000_000, 99_999_999)}"


@pytest.fixture()
def hospital(db_session):
    h = Hospital(name=f"테스트병원-{random.randint(1000, 999999)}")
    db_session.add(h)
    db_session.commit()
    db_session.refresh(h)
    return h


@pytest.fixture()
def doctor_user(db_session, hospital):
    u = User(
        phone_number=_rand_phone(),
        password_hash=hash_password("test1234"),
        name="테스트의사",
        role=UserRole.doctor,
        birth_date="1980-01-01",
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)

    db_session.add(DoctorProfile(
        user_id=u.id,
        birth_date="1980-01-01",
        license_number=f"TEST-LIC-{u.id}",
        hospital_id=hospital.id,
    ))
    db_session.commit()
    return u


@pytest.fixture()
def patient_user(db_session):
    u = User(
        phone_number=_rand_phone(),
        password_hash=hash_password("test1234"),
        name="테스트환자",
        role=UserRole.patient,
        birth_date="1960-05-05",
        gender="m",
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def assigned_patient(db_session, doctor_user, patient_user):
    """patient_user를 doctor_user의 현재 담당 환자로 연결."""
    patient_user.doctor_id = doctor_user.id
    db_session.add(PatientDoctorAssignment(patient_id=patient_user.id, doctor_id=doctor_user.id))
    db_session.commit()
    db_session.refresh(patient_user)
    return patient_user


@pytest.fixture()
def make_auth_headers():
    """user 객체 -> Authorization 헤더. /login 없이 바로 유효한 액세스 토큰 발급."""

    def _make(user: User) -> dict:
        token = create_access_token({"sub": str(user.id), "role": user.role})
        return {"Authorization": f"Bearer {token}"}

    return _make
