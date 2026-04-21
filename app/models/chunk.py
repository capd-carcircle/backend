"""
KDIGO 문서 청크 + 임베딩 저장 테이블
- pgvector의 Vector 타입으로 384차원 임베딩 저장
- 1회성 ingest 스크립트로 채워지고, 이후 RAG 검색에 사용됨
"""

from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # PDF 파일명 (예: "KDIGO-2021-BP-GL.pdf")
    source: Mapped[str] = mapped_column(String(200), nullable=False, index=True)

    # PDF 페이지 번호 (1-indexed)
    page_num: Mapped[int] = mapped_column(Integer, nullable=True)

    # 청크 원문 텍스트
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)

    # all-MiniLM-L6-v2 임베딩 (384차원)
    embedding = mapped_column(Vector(384), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
