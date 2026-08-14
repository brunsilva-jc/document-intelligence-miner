"""Acesso a dados de documentos e chunks.

Toda consulta SQL vive aqui: services falam com o repositorio, nunca com
o SQLAlchemy diretamente. Isso permite trocar o storage sem tocar na
regra de negocio e testar services com um fake de repositorio.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import Document, DocumentChunk, DocumentStatus


@dataclass(slots=True)
class ChunkMatch:
    """Chunk recuperado pela busca vetorial, com sua distancia."""

    chunk: DocumentChunk
    filename: str
    # Distancia de cosseno: 0 = identico, 1 = ortogonal, 2 = oposto.
    distance: float

    @property
    def score(self) -> float:
        """Similaridade de cosseno (1.0 = identico), mais intuitiva na API."""
        return 1.0 - self.distance


class DocumentRepository:
    """Repositorio de `Document` e `DocumentChunk`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------------- Document ----------------

    async def create(
        self,
        *,
        filename: str,
        content_type: str,
        size_bytes: int,
        checksum: str,
        metadata: dict | None = None,
    ) -> Document:
        document = Document(
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            checksum=checksum,
            status=DocumentStatus.PENDING,
            doc_metadata=metadata or {},
        )
        self._session.add(document)
        await self._session.flush()
        return document

    async def get_by_id(self, document_id: uuid.UUID) -> Document | None:
        return await self._session.get(Document, document_id)

    async def get_by_checksum(self, checksum: str) -> Document | None:
        stmt = select(Document).where(Document.checksum == checksum)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    # Nome `list_all` (e nao `list`) para nao sombrear o builtin dentro
    # do corpo da classe, onde ele e usado em anotacoes.
    async def list_all(self, *, limit: int = 50, offset: int = 0) -> Sequence[Document]:
        stmt = select(Document).order_by(Document.created_at.desc()).limit(limit).offset(offset)
        return (await self._session.execute(stmt)).scalars().all()

    async def count(self) -> int:
        stmt = select(func.count()).select_from(Document)
        return (await self._session.execute(stmt)).scalar_one()

    async def mark_status(
        self,
        document: Document,
        status: DocumentStatus,
        *,
        error_message: str | None = None,
        chunk_count: int | None = None,
    ) -> Document:
        document.status = status
        document.error_message = error_message
        if chunk_count is not None:
            document.chunk_count = chunk_count
        await self._session.flush()
        return document

    async def delete(self, document_id: uuid.UUID) -> bool:
        """Remove o documento e, em cascata, seus chunks."""
        stmt = delete(Document).where(Document.id == document_id)
        result = await self._session.execute(stmt)
        return bool(result.rowcount)

    # ---------------- DocumentChunk ----------------

    async def add_chunks(self, chunks: Sequence[DocumentChunk]) -> int:
        """Insere os chunks ja vetorizados de um documento."""
        if not chunks:
            return 0
        self._session.add_all(chunks)
        await self._session.flush()
        return len(chunks)

    async def count_chunks(self, document_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
        )
        return (await self._session.execute(stmt)).scalar_one()

    # ---------------- Busca semantica ----------------

    async def search_similar_chunks(
        self,
        embedding: Sequence[float],
        *,
        top_k: int,
        max_distance: float | None = None,
        document_ids: Sequence[uuid.UUID] | None = None,
    ) -> list[ChunkMatch]:
        """Retorna os chunks mais proximos do vetor da pergunta.

        Usa distancia de cosseno (`<=>` do pgvector), servida pelo indice
        HNSW criado em `DocumentChunk.__table_args__`. O `ORDER BY` sobre
        a mesma expressao do indice e o que permite o plano usar ANN em
        vez de varrer a tabela.
        """
        distance = DocumentChunk.embedding.cosine_distance(embedding)

        stmt = (
            select(DocumentChunk, Document.filename, distance.label("distance"))
            .join(Document, Document.id == DocumentChunk.document_id)
            .order_by(distance)
            .limit(top_k)
        )

        if document_ids:
            stmt = stmt.where(DocumentChunk.document_id.in_(document_ids))
        if max_distance is not None:
            stmt = stmt.where(distance <= max_distance)

        rows = (await self._session.execute(stmt)).all()
        return [
            ChunkMatch(chunk=chunk, filename=filename, distance=float(dist))
            for chunk, filename, dist in rows
        ]
