from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import api_router
from app.core.auth import hash_password
from app.core.database import Base, SessionLocal, engine
from app.models.user import User, UserRole
from app.models import record as _record_models  # noqa: F401 – daily_records, exchange_records 테이블 생성용


def _seed_dev_data():
    """개발 환경 테스트 계정 자동 생성"""
    db = SessionLocal()
    try:
        test_users = [
            {"email": "doctor@test.com",  "name": "테스트 의사", "role": UserRole.doctor,  "password": "test1234"},
            {"email": "patient@test.com", "name": "테스트 환자", "role": UserRole.patient, "password": "test1234"},
        ]
        for u in test_users:
            exists = db.query(User).filter(User.email == u["email"]).first()
            if not exists:
                db.add(User(
                    email=u["email"],
                    name=u["name"],
                    role=u["role"],
                    password_hash=hash_password(u["password"]),
                ))
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시
    Base.metadata.create_all(bind=engine)  # 테이블 자동 생성
    _seed_dev_data()                        # 테스트 계정 생성
    yield
    # 서버 종료 시 (필요 시 정리 로직)


app = FastAPI(
    title="CAPD API",
    description="CAPD 일일 기록 검토 및 AI 기반 후속 질문 지원 시스템",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS 설정 (개발 환경)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/health")
def health_check():
    return {"status": "ok"}
