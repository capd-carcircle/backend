# CAPD Backend

CAPD(복막투석) 일일 기록 검토 및 AI 기반 후속 질문 지원 시스템의 백엔드 서버입니다.

## 기술 스택

- **Framework:** FastAPI + Uvicorn
- **DB:** PostgreSQL 16 + pgvector (벡터 유사도 검색)
- **ORM:** SQLAlchemy 2.0 + Alembic (마이그레이션)
- **Auth:** JWT (python-jose)
- **AI:** LM Studio (Qwen2.5-3B, 로컬) → GCP 전환 시 Gemini로 교체 예정
- **RAG:** sentence-transformers (all-MiniLM-L6-v2, 384차원) + pgvector
- **배포 예정:** GCP Cloud Run + Cloud SQL

---

## 로컬 개발 환경 실행

### 사전 준비
- Docker Desktop 설치 및 실행
- `.env` 파일 준비 (`.env.example` 참고)

### 실행
```bash
# 프로젝트 루트(CAPD/)에서 실행
docker-compose up --build
```

| 서비스 | 주소 |
|---|---|
| FastAPI 서버 | http://localhost:8000 |
| API 문서 (Swagger) | http://localhost:8000/docs |
| PostgreSQL | localhost:5432 |

### 종료 / 초기화
```bash
docker-compose down          # 종료
docker-compose down -v       # 종료 + 볼륨(DB 데이터) 삭제
```

---

## 폴더 구조

```
backend/
├── app/
│   ├── api/v1/routes/       # API 라우터
│   │   ├── auth.py          # 로그인 / 토큰 발급
│   │   ├── records.py       # 환자 투석 기록 CRUD
│   │   ├── surveys.py       # AI 맞춤 질문 생성 + 설문 응답
│   │   ├── dashboard.py     # 의사 대시보드 (환자 목록 + 기록 요약)
│   │   ├── questions.py     # 의사: 공통 질문 CRUD
│   │   └── ai.py            # AI 관련 엔드포인트
│   ├── models/              # SQLAlchemy ORM 모델
│   │   ├── user.py          # User (환자/의사 공용)
│   │   ├── record.py        # DailyRecord (투석 일일 기록)
│   │   ├── question.py      # AIQuestion, CommonQuestion
│   │   ├── survey.py        # SurveyResponse, RejectedQPattern
│   │   └── chunk.py         # DocumentChunk (KDIGO RAG 청크)
│   ├── schemas/             # Pydantic 요청/응답 스키마
│   ├── services/            # 비즈니스 로직
│   │   ├── ai_service.py    # LM Studio 질문 생성 (OpenAI 호환 API)
│   │   ├── rag_service.py   # pgvector 기반 KDIGO 문서 검색
│   │   ├── record_service.py
│   │   └── question_service.py
│   └── core/
│       ├── auth.py          # JWT 인증 미들웨어
│       ├── config.py        # 환경변수 설정
│       └── database.py      # DB 세션 관리
├── scripts/
│   ├── ingest_kdigo.py      # KDIGO PDF → 청크 → 임베딩 → DB 저장 (1회성)
│   └── test_rag.py          # RAG 파이프라인 동작 확인 스크립트
├── Dockerfile
└── requirements.txt
```

---

## API 엔드포인트 요약

| Method | 경로 | 설명 | 권한 |
|---|---|---|---|
| POST | `/api/v1/auth/login` | 로그인 (JWT 발급) | - |
| GET | `/api/v1/records/` | 내 기록 목록 | 환자 |
| POST | `/api/v1/records/` | 오늘 기록 제출 | 환자 |
| PUT | `/api/v1/records/{id}` | 오늘 기록 수정 | 환자 |
| GET | `/api/v1/surveys/ai-questions/{record_id}` | AI 맞춤 질문 조회/생성 | 환자 |
| POST | `/api/v1/surveys/responses` | 설문 응답 제출 | 환자 |
| GET | `/api/v1/surveys/responses/{record_id}` | 설문 응답 조회 | 의사 |
| GET | `/api/v1/dashboard` | 환자 목록 + 최근 기록 요약 | 의사 |
| GET | `/api/v1/questions/` | 공통 질문 목록 | 의사/환자 |
| POST | `/api/v1/questions/` | 공통 질문 추가 | 의사 |
| PUT | `/api/v1/questions/{id}` | 공통 질문 수정 | 의사 |
| DELETE | `/api/v1/questions/{id}` | 공통 질문 삭제 | 의사 |

전체 상세 문서: http://localhost:8000/docs

---

## AI / RAG 구조

```
환자 기록 제출
    ↓
surveys.py: _generate_ai_questions()
    ↓
[1단계] 규칙 기반 탐지 (KDIGO 5가지 기준)
    - 한외여과 감소 / 고혈압 / 혼탁 투석액 / 체중 증가 / 고혈당
    ↓
[2단계] 규칙으로 부족하면 RAG + LM Studio 보완
    rag_service.py: 환자 기록 → 영어 쿼리 변환 → pgvector top-3 검색
    ai_service.py:  KDIGO 문단 + 기록을 Qwen2.5-3B에 전달 → 질문 생성
    ↓
AI 맞춤 질문 저장 (ai_questions 테이블)
```

### KDIGO 인제스트 (최초 1회)
Docker OOM 문제로 **Windows Python으로 직접 실행** 권장:
```powershell
# 프로젝트 루트에서
python -m venv ingest_env
.\ingest_env\Scripts\Activate.ps1
pip install sentence-transformers pypdf sqlalchemy psycopg2-binary python-dotenv pgvector pydantic-settings pydantic[email] email-validator openai

$env:DATABASE_URL = "postgresql://capd_user:capd_pass@localhost:5432/capd"
python backend/scripts/ingest_kdigo.py
```
성공 시 `document_chunks` 테이블에 361개 청크 저장됨.

### RAG 동작 확인
```powershell
python backend/scripts/test_rag.py
```

---

## 환경변수 (.env)

```env
DATABASE_URL=postgresql://capd_user:capd_pass@localhost:5432/capd
SECRET_KEY=your-secret-key
POSTGRES_DB=capd
POSTGRES_USER=capd_user
POSTGRES_PASSWORD=capd_pass
```

---

## GCP 전환 시 변경 사항 (예정)

| 현재 (로컬) | GCP 전환 후 |
|---|---|
| LM Studio (Qwen2.5-3B) | Gemini API |
| sentence-transformers | Vertex AI Embedding API |
| Docker PostgreSQL | Cloud SQL (pgvector) |
| 로컬 PDF 파일 | Cloud Storage |
| Docker | Cloud Run |
