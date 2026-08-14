"""Modelos ORM: documentos e seus chunks vetorizados."""

import uuid
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.db.base import Base, TimestampMixin


class DocumentStatus(StrEnum):
    """Ciclo de vida da ingestao de um documento."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Document(TimestampMixin, Base):
    """Metadados do arquivo enviado. O conteudo vive nos chunks."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # SHA-256 do arquivo: permite deduplicar reenvios do mesmo conteudo.
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[DocumentStatus] = mapped_column(
        SAEnum(DocumentStatus, name="document_status", native_enum=False, length=20),
        nullable=False,
        default=DocumentStatus.PENDING,
        index=True,
    )
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_metadata: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - auxilio a depuracao
        return f"<Document id={self.id} filename={self.filename!r} status={self.status}>"


class DocumentChunk(TimestampMixin, Base):
    """Bloco de texto + embedding, unidade de busca semantica."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_chunk_position"),
        # Indice ANN para busca por cosseno. HNSW tem recall melhor que
        # IVFFlat e nao exige treino previo com dados na tabela.
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # A dimensao e fixa no schema: trocar de modelo de embedding exige
    # migracao + reindexacao de todo o acervo.
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.EMBEDDING_DIM), nullable=False)
    chunk_metadata: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    document: Mapped[Document] = relationship(back_populates="chunks")

    def __repr__(self) -> str:  # pragma: no cover - auxilio a depuracao
        return f"<DocumentChunk doc={self.document_id} idx={self.chunk_index}>"
