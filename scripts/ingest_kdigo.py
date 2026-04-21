"""
KDIGO PDF 청킹 + 임베딩 → DB 저장 (1회성 스크립트)

사용법:
  # 도커 컨테이너 안에서 실행
  docker exec -it capd_backend python scripts/ingest_kdigo.py

  # 또는 로컬에서 (venv 활성화 후)
  python backend/scripts/ingest_kdigo.py

환경 변수:
  DATABASE_URL — .env 파일에서 자동 로드됨
  KDIGO_DIR    — KDIGO PDF 폴더 경로 (기본: 프로젝트 루트의 26년-capd-materials/KDIGO)
"""

import gc
import os
import sys
import textwrap
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가 (스크립트를 어디서 실행해도 동작)
BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

# .env 로드
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

from pypdf import PdfReader
from sentence_transformers import SentenceTransformer
from sqlalchemy.orm import Session

from app.core.database import engine
from app.models.chunk import DocumentChunk

# ── 설정 ─────────────────────────────────────────────────────
# 컨테이너 안: /app/capd-materials/KDIGO (docker-compose 볼륨 마운트)
# 로컬 실행:  프로젝트 루트 / 26년-capd-materials / KDIGO
_CONTAINER_PATH = BACKEND_ROOT / "capd-materials" / "KDIGO"
_LOCAL_PATH = PROJECT_ROOT / "26년-capd-materials" / "KDIGO"
KDIGO_DIR = Path(os.getenv("KDIGO_DIR", _CONTAINER_PATH if _CONTAINER_PATH.exists() else _LOCAL_PATH))
EMBED_MODEL = "all-MiniLM-L6-v2"   # 384차원, 영어 특화, 80MB
CHUNK_SIZE  = 500                   # 청크 최대 글자 수
CHUNK_OVERLAP = 50                  # 청크 간 겹침 글자 수
BATCH_SIZE  = 8                     # 임베딩 배치 크기 (메모리 절약을 위해 작게 유지)


def extract_text_by_page(pdf_path: Path) -> list[tuple[int, str]]:
    """PDF에서 페이지별 텍스트 추출. [(page_num, text), ...]"""
    reader = PdfReader(str(pdf_path))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        text = text.strip()
        if text:
            pages.append((i, text))
    return pages


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """텍스트를 고정 크기 청크로 분할. 문장 경계를 최대한 존중."""
    # 줄바꿈 정규화
    text = " ".join(text.split())

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size

        if end >= len(text):
            chunks.append(text[start:].strip())
            break

        # 문장 끝(. ! ?)을 찾아서 그 지점에서 자름
        cut = end
        for sep in (". ", "! ", "? "):
            pos = text.rfind(sep, start, end)
            if pos != -1:
                cut = pos + 1
                break

        chunk = text[start:cut].strip()
        if chunk:
            chunks.append(chunk)

        new_start = cut - overlap  # overlap만큼 뒤로 이동
        if new_start <= start:    # 무한 루프 방지: 항상 앞으로 전진
            new_start = cut
        start = new_start

    return [c for c in chunks if len(c) > 50]  # 너무 짧은 조각 제거


def ingest(clear_existing: bool = False):
    """메인 ingest 로직"""
    if not KDIGO_DIR.exists():
        logger.error(f"KDIGO 폴더를 찾을 수 없습니다: {KDIGO_DIR}")
        sys.exit(1)

    pdf_files = list(KDIGO_DIR.glob("*.pdf"))
    if not pdf_files:
        logger.error(f"PDF 파일이 없습니다: {KDIGO_DIR}")
        sys.exit(1)

    logger.info(f"PDF {len(pdf_files)}개 발견: {[f.name for f in pdf_files]}")

    # 임베딩 모델 로드
    logger.info(f"임베딩 모델 로드 중: {EMBED_MODEL}")
    model = SentenceTransformer(EMBED_MODEL)

    with Session(engine) as db:
        if clear_existing:
            deleted = db.query(DocumentChunk).delete()
            db.commit()
            logger.info(f"기존 청크 {deleted}개 삭제 완료")

        total_saved = 0

        for pdf_path in sorted(pdf_files):
            source_name = pdf_path.name
            logger.info(f"\n[{source_name}] 처리 시작")

            # 이미 인제스트된 파일 스킵
            existing = db.query(DocumentChunk).filter(
                DocumentChunk.source == source_name
            ).count()
            if existing > 0:
                logger.info(f"  → 이미 인제스트됨 ({existing}개 청크). 스킵. (재처리하려면 --clear 옵션 사용)")
                continue

            # 텍스트 추출
            pages = extract_text_by_page(pdf_path)
            logger.info(f"  → {len(pages)}개 페이지 추출")

            # 청킹
            all_chunks: list[tuple[int, str]] = []  # [(page_num, chunk_text)]
            for page_num, page_text in pages:
                for chunk in chunk_text(page_text):
                    all_chunks.append((page_num, chunk))

            logger.info(f"  → {len(all_chunks)}개 청크 생성, 임베딩 시작 (배치 크기: {BATCH_SIZE})")

            # 스트리밍 배치 임베딩 + 즉시 DB 저장 (메모리 절약)
            saved = 0
            for i in range(0, len(all_chunks), BATCH_SIZE):
                batch = all_chunks[i:i + BATCH_SIZE]
                texts = [c[1] for c in batch]
                embeddings = model.encode(
                    texts,
                    batch_size=BATCH_SIZE,
                    show_progress_bar=False,
                    normalize_embeddings=True,
                )
                for (page_num, chunk_txt), emb in zip(batch, embeddings):
                    db.add(DocumentChunk(
                        source=source_name,
                        page_num=page_num,
                        chunk_text=chunk_txt,
                        embedding=emb.tolist(),
                    ))
                db.commit()
                saved += len(batch)
                del embeddings, texts, batch
                gc.collect()
                if saved % 50 == 0 or saved == len(all_chunks):
                    logger.info(f"  → {saved}/{len(all_chunks)}개 처리 완료")

            logger.info(f"  → {saved}개 청크 저장 완료")
            total_saved += saved

    logger.info(f"\n✅ 인제스트 완료: 총 {total_saved}개 청크 저장")


if __name__ == "__main__":
    clear = "--clear" in sys.argv
    if clear:
        logger.info("--clear 옵션: 기존 청크를 모두 삭제하고 재처리합니다.")
    ingest(clear_existing=clear)
