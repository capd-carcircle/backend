from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import api_router
from app.core.auth import hash_password
from app.core.database import Base, SessionLocal, engine
from app.models.user import User, UserRole
from app.models import record as _record_models   # noqa: F401
from app.models import survey as _survey_models   # noqa: F401
from app.models import question as _question_models  # noqa: F401
from app.models import chunk as _chunk_models        # noqa: F401
from app.models import hospital as _hospital_models  # noqa: F401
from app.models import registration as _registration_models  # noqa: F401
from app.models.hospital import Hospital, DoctorLicense, DoctorProfile
from app.models.registration import PatientRegistration, RegistrationStatus


def _seed_dev_data():
    """개발 환경 시드 데이터 자동 생성"""
    db = SessionLocal()
    try:
        # ── 1. 병원 시드 ──────────────────────────────────────────
        hospitals_data = [
            {"name": "서울대학교병원", "address": "서울특별시 종로구 대학로 101"},
            {"name": "세브란스병원",   "address": "서울특별시 서대문구 연세로 50-1"},
            {"name": "삼성서울병원",   "address": "서울특별시 강남구 일원로 81"},
            {"name": "서울아산병원",   "address": "서울특별시 송파구 올림픽로43길 88"},
            {"name": "가톨릭대학교 서울성모병원", "address": "서울특별시 서초구 반포대로 222"},
        ]
        for h_data in hospitals_data:
            if not db.query(Hospital).filter_by(name=h_data["name"]).first():
                db.add(Hospital(**h_data))
        db.flush()

        severance = db.query(Hospital).filter_by(name="세브란스병원").first()
        seoul_h   = db.query(Hospital).filter_by(name="서울대학교병원").first()

        # ── 2. 자격번호 시드 ──────────────────────────────────────
        licenses_data = [
            {"name": "테스트의사", "birth_date": "1980-01-15",
             "license_number": "NEPH-2024-001", "hospital_id": severance.id if severance else None},
            {"name": "김철수", "birth_date": "1975-06-20",
             "license_number": "NEPH-2024-002", "hospital_id": seoul_h.id if seoul_h else None},
        ]
        for lic_data in licenses_data:
            if not db.query(DoctorLicense).filter_by(license_number=lic_data["license_number"]).first():
                db.add(DoctorLicense(**lic_data))
        db.flush()

        # ── 3. 개발용 테스트 계정 ────────────────────────────────
        test_users = [
            {"phone_number": "01011112222", "name": "테스트의사",
             "role": UserRole.doctor,  "password": "test1234", "birth_date": "1980-01-15"},
            {"phone_number": "01033334444", "name": "테스트환자",
             "role": UserRole.patient, "password": "test1234", "birth_date": "1990-05-10"},
        ]
        for u in test_users:
            if not db.query(User).filter_by(phone_number=u["phone_number"]).first():
                db.add(User(
                    phone_number=u["phone_number"],
                    name=u["name"],
                    role=u["role"],
                    birth_date=u["birth_date"],
                    password_hash=hash_password(u["password"]),
                ))
        db.flush()

        # ── 4. 테스트 의사 → DoctorProfile(세브란스병원) ─────────
        doctor = db.query(User).filter_by(phone_number="01011112222").first()
        if doctor and severance:
            if not db.query(DoctorProfile).filter_by(user_id=doctor.id).first():
                db.add(DoctorProfile(
                    user_id=doctor.id,
                    birth_date="1980-01-15",
                    license_number="NEPH-2024-001",
                    hospital_id=severance.id,
                ))
                # 자격번호도 사용됨으로 표시
                lic = db.query(DoctorLicense).filter_by(license_number="NEPH-2024-001").first()
                if lic:
                    lic.is_registered = True
        db.flush()

        # ── 5. 모든 환자 → 테스트 의사에게 patient_registrations 연결 ──
        if doctor and severance:
            all_patients = db.query(User).filter(
                User.role == UserRole.patient, User.is_active == True
            ).all()

            for patient in all_patients:
                already = db.query(PatientRegistration).filter_by(
                    doctor_id=doctor.id,
                    user_id=patient.id,
                    status=RegistrationStatus.completed,
                ).first()
                if not already:
                    db.add(PatientRegistration(
                        name=patient.name,
                        birth_date=patient.birth_date or "2000-01-01",
                        hospital_id=severance.id,
                        doctor_id=doctor.id,
                        status=RegistrationStatus.completed,
                        user_id=patient.id,
                    ))

        db.commit()
        print("[startup] 시드 데이터 완료")
        print("[startup] 의사: 01011112222 / test1234  |  환자: 01033334444 / test1234")
    except Exception as e:
        db.rollback()
        print(f"[startup] 시딩 오류: {e}")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[startup] DB 테이블 생성 중...")
    Base.metadata.create_all(bind=engine)
    print("[startup] DB 테이블 생성 완료")
    _seed_dev_data()
    yield


app = FastAPI(
    title="CAPD API",
    description="CAPD 일일 기록 검토 및 AI 기반 후속 질문 지원 시스템",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://13.55.214.191:5173", "http://13.55.214.191:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/health")
def health_check():
    return {"status": "ok"}
