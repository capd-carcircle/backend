"""
RAG 동작 확인 스크립트
실행: python backend/scripts/test_rag.py
(ingest_env 또는 프로젝트 venv 활성화 상태에서)
"""
import os
import sys
import json

# backend/ 폴더를 sys.path에 추가 (app 모듈 인식용)
BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://capd_user:capd_pass@localhost:5432/capd")

# ── 1. DB 청크 수 확인 ──────────────────────────────────────
print("=" * 60)
print("1. document_chunks 테이블 청크 수 확인")
print("=" * 60)
try:
    from sqlalchemy import create_engine, text
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM document_chunks")).scalar()
        sources = conn.execute(
            text("SELECT source, COUNT(*) as cnt FROM document_chunks GROUP BY source ORDER BY cnt DESC")
        ).fetchall()
    print(f"✅ 총 청크 수: {count}")
    print("소스별:")
    for src, cnt in sources:
        print(f"  - {src}: {cnt}개")
except Exception as e:
    print(f"❌ DB 연결 실패: {e}")
    sys.exit(1)

# ── 2. 임베딩 모델 로드 ─────────────────────────────────────
print("\n" + "=" * 60)
print("2. 임베딩 모델 로드 (all-MiniLM-L6-v2)")
print("=" * 60)
try:
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("all-MiniLM-L6-v2")
    test_vec = model.encode("test", normalize_embeddings=True)
    print(f"✅ 모델 로드 성공 (벡터 차원: {len(test_vec)})")
except Exception as e:
    print(f"❌ 모델 로드 실패: {e}")
    sys.exit(1)

# ── 3. RAG 검색 테스트 ──────────────────────────────────────
print("\n" + "=" * 60)
print("3. RAG 검색 테스트 (고혈압 + 혼탁 투석액 가상 기록)")
print("=" * 60)
test_record = {
    "weight": 65.5,
    "blood_pressure": "155/95",
    "total_uf": 800,
    "turbid": True,
    "blood_sugar": 145,
    "memo": "오늘 좀 붓는 느낌",
    "recent_uf_7d": [800, 950, 1100, 1200, 1300, 1250, 1100],
}
try:
    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=engine)
    db = Session()

    from app.services.rag_service import search_kdigo_context, _record_to_query
    query_text = _record_to_query(test_record)
    print(f"생성된 검색 쿼리:\n  {query_text}\n")

    context = search_kdigo_context(test_record, db)
    if context:
        print(f"✅ KDIGO 컨텍스트 검색 성공\! ({len(context)}자)")
        print("-" * 40)
        print(context[:800] + ("..." if len(context) > 800 else ""))
    else:
        print("⚠️  컨텍스트 없음 (청크가 비어있거나 검색 실패)")
    db.close()
except Exception as e:
    print(f"❌ RAG 검색 실패: {e}")

# ── 4. LM Studio 연결 확인 ──────────────────────────────────
print("\n" + "=" * 60)
print("4. LM Studio 연결 확인 (localhost:1234)")
print("=" * 60)
try:
    from openai import OpenAI
    client = OpenAI(base_url="http://localhost:1234/v1", api_key="lm-studio")
    models = client.models.list()
    print(f"✅ LM Studio 연결 성공\!")
    for m in models.data:
        print(f"  - 로드된 모델: {m.id}")
except Exception as e:
    print(f"⚠️  LM Studio 연결 안 됨 (꺼진 상태면 정상): {e}")

# ── 5. 전체 파이프라인 테스트 (LM Studio 켜진 경우) ─────────
print("\n" + "=" * 60)
print("5. 전체 파이프라인: RAG + LM Studio 질문 생성")
print("=" * 60)
try:
    from app.services.ai_service import generate_questions_via_lm_studio
    from app.services.rag_service import search_kdigo_context
    from sqlalchemy.orm import sessionmaker
    Session2 = sessionmaker(bind=engine)
    db2 = Session2()

    kdigo_ctx = search_kdigo_context(test_record, db2)
    questions = generate_questions_via_lm_studio(
        test_record,
        rejected_keys=[],
        kdigo_context=kdigo_ctx
    )
    db2.close()

    if questions:
        print(f"✅ LM Studio 질문 생성 성공\!")
        for i, q in enumerate(questions, 1):
            print(f"\n  [{i}] 질문: {q.get('question_text')}")
            print(f"       이유: {q.get('reason')}")
    else:
        print("⚠️  질문 생성 없음 (LM Studio 꺼져있거나 응답 파싱 실패)")
except Exception as e:
    print(f"❌ 파이프라인 테스트 실패: {e}")

print("\n" + "=" * 60)
print("테스트 완료")
print("=" * 60)
