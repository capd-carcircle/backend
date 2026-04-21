"""
RAG 검색 서비스
- 환자 기록을 임베딩으로 변환
- pgvector <=> 연산자로 관련 KDIGO 청크 검색
- 결과 텍스트를 LM Studio 프롬프트에 주입할 형태로 반환
"""

import logging
from functools import lru_cache

from sentence_transformers import SentenceTransformer
from sqlalchemy.orm import Session

from app.models.chunk import DocumentChunk

logger = logging.getLogger(__name__)

EMBED_MODEL = "all-MiniLM-L6-v2"
TOP_K = 3  # 프롬프트에 주입할 KDIGO 청크 수


@lru_cache(maxsize=1)
def _get_model() -> SentenceTransformer:
    """임베딩 모델 싱글턴 (최초 1회만 로드)"""
    logger.info(f"임베딩 모델 로드: {EMBED_MODEL}")
    return SentenceTransformer(EMBED_MODEL)


def _record_to_query(record_data: dict) -> str:
    """
    환자 기록 dict → 영어 검색 쿼리 문자열 변환
    (KDIGO PDF가 영어이므로 영어로 변환해서 검색)
    """
    parts = []

    bp = record_data.get("blood_pressure")
    if bp:
        try:
            systolic = int(bp.split("/")[0])
            if systolic > 140:
                parts.append(f"high blood pressure {bp} mmHg hypertension management CAPD")
        except Exception:
            pass

    uf = record_data.get("total_uf")
    if uf is not None:
        parts.append(f"ultrafiltration volume {uf} ml peritoneal dialysis")

    if record_data.get("turbid"):
        parts.append("cloudy dialysate turbid peritoneal peritonitis diagnosis")

    weight = record_data.get("weight")
    if weight:
        parts.append(f"fluid overload weight gain {weight} kg edema CAPD")

    glucose = record_data.get("blood_sugar")
    if glucose and glucose > 180:
        parts.append(f"fasting blood glucose {glucose} mg/dL diabetes CKD management")

    # 기록이 거의 없으면 일반 CAPD 쿼리
    if not parts:
        parts.append("CAPD peritoneal dialysis patient monitoring guidelines")

    return " ".join(parts)


def search_kdigo_context(record_data: dict, db: Session, top_k: int = TOP_K) -> str:
    """
    환자 기록 기반으로 관련 KDIGO 문단을 검색하고
    프롬프트에 주입할 텍스트 블록을 반환.

    Returns:
        KDIGO 관련 문단들을 합친 문자열.
        DB에 청크가 없거나 오류 시 빈 문자열 반환.
    """
    try:
        # 청크 존재 여부 빠른 확인
        count = db.query(DocumentChunk).limit(1).count()
        if count == 0:
            logger.warning("document_chunks 테이블이 비어 있습니다. ingest_kdigo.py를 먼저 실행하세요.")
            return ""

        model = _get_model()
        query_text = _record_to_query(record_data)
        query_vec = model.encode(query_text, normalize_embeddings=True).tolist()

        # pgvector 코사인 거리 검색 (<=> 연산자)
        results = (
            db.query(DocumentChunk)
            .order_by(DocumentChunk.embedding.op("<=>")(query_vec))
            .limit(top_k)
            .all()
        )

        if not results:
            return ""

        chunks = []
        for i, chunk in enumerate(results, start=1):
            source = chunk.source.replace(".pdf", "").replace("-", " ")
            chunks.append(f"[{i}] ({source}, p.{chunk.page_num})\n{chunk.chunk_text}")

        context = "\n\n".join(chunks)
        logger.info(f"KDIGO 컨텍스트 {len(results)}개 청크 검색 완료")
        return context

    except Exception as e:
        logger.warning(f"RAG 검색 실패 (무시하고 계속): {e}")
        return ""
