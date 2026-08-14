"""Regras de negocio de documentos: ingestao, consulta e remocao."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.config import settings
from app.core.exceptions import DocumentNotFoundError
from app.core.logging import get_logger
from app.models.domain import Document, DocumentChunk, DocumentStatus
from app.repositories.document_repository import DocumentRepository
from app.services.document_processor import DocumentProcessor, TextChunk
from app.services.embeddings import EmbeddingProvider, validate_dimension

logger = get_logger(__name__)

# Lote enviado por chamada ao provedor de embeddings. Lotes grandes
# reduzem round-trips; grandes demais estouram o limite de tokens da API.
EMBEDDING_BATCH_SIZE = 64


@dataclass(slots=True)
class IngestionResult:
    document: Document
    chunks_created: int
    duplicated: bool


class DocumentService:
    """Orquestra casos de uso sobre documentos."""

    def __init__(
        self,
        repository: DocumentRepository,
        processor: DocumentProcessor | None = None,
        embeddings: EmbeddingProvider | None = None,
    ) -> None:
        self._repository = repository
        self._processor = processor or DocumentProcessor()
        self._embeddings = embeddings

    # ------------------------------------------------------------------
    # Ingestao
    # ------------------------------------------------------------------
    async def ingest(
        self,
        *,
        filename: str,
        content_type: str | None,
        data: bytes,
    ) -> IngestionResult:
        """Valida, extrai, divide, vetoriza e persiste um arquivo.

        A ordem importa: todo o processamento acontece antes de qualquer
        escrita, e documento e chunks sao gravados na mesma transacao.
        Uma falha no meio do caminho nao deixa documento orfao no banco.
        """
        extension = self._processor.validate(filename, content_type, len(data))
        checksum = self._processor.checksum(data)

        existing = await self._repository.get_by_checksum(checksum)
        if existing is not None:
            logger.info("upload ignorado, checksum ja ingerido: %s", filename)
            return IngestionResult(
                document=existing,
                chunks_created=await self._repository.count_chunks(existing.id),
                duplicated=True,
            )

        extracted = await self._processor.extract(data, extension)
        text_chunks = self._processor.chunk(extracted)
        vectors = await self._embed_in_batches([chunk.content for chunk in text_chunks])

        document = await self._repository.create(
            filename=filename,
            content_type=content_type or "application/octet-stream",
            size_bytes=len(data),
            checksum=checksum,
            metadata={
                "pages": len(extracted.pages),
                "characters": len(extracted.full_text),
                "embedding_model": self._embedding_provider.model_name,
                "chunk_size": settings.CHUNK_SIZE,
                "chunk_overlap": settings.CHUNK_OVERLAP,
            },
        )

        created = await self._repository.add_chunks(
            self._to_orm_chunks(document.id, text_chunks, vectors)
        )
        await self._repository.mark_status(document, DocumentStatus.COMPLETED, chunk_count=created)

        logger.info("documento ingerido: %s (%d chunks)", filename, created)
        return IngestionResult(document=document, chunks_created=created, duplicated=False)

    async def _embed_in_batches(self, texts: list[str]) -> list[list[float]]:
        """Vetoriza em lotes e valida a dimensao antes de tocar no banco."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = texts[start : start + EMBEDDING_BATCH_SIZE]
            vectors.extend(await self._embedding_provider.embed_documents(batch))

        validate_dimension(vectors, settings.EMBEDDING_DIM)
        return vectors

    @staticmethod
    def _to_orm_chunks(
        document_id: uuid.UUID,
        text_chunks: Sequence[TextChunk],
        vectors: Sequence[Sequence[float]],
    ) -> list[DocumentChunk]:
        return [
            DocumentChunk(
                document_id=document_id,
                chunk_index=chunk.index,
                content=chunk.content,
                token_count=chunk.token_count,
                embedding=list(vector),
                chunk_metadata=chunk.metadata,
            )
            for chunk, vector in zip(text_chunks, vectors, strict=True)
        ]

    @property
    def _embedding_provider(self) -> EmbeddingProvider:
        """Resolve o provedor tardiamente: listar documentos nao deve
        exigir chave de API nem carregar modelo em memoria."""
        if self._embeddings is None:
            from app.services.embeddings import get_embedding_provider

            self._embeddings = get_embedding_provider()
        return self._embeddings

    # ------------------------------------------------------------------
    # Consulta e remocao
    # ------------------------------------------------------------------
    async def list_documents(
        self, *, limit: int = 50, offset: int = 0
    ) -> tuple[int, Sequence[Document]]:
        total = await self._repository.count()
        items = await self._repository.list_all(limit=limit, offset=offset)
        return total, items

    async def get_document(self, document_id: uuid.UUID) -> Document:
        document = await self._repository.get_by_id(document_id)
        if document is None:
            raise DocumentNotFoundError(f"Documento {document_id} nao encontrado.")
        return document

    async def delete_document(self, document_id: uuid.UUID) -> None:
        deleted = await self._repository.delete(document_id)
        if not deleted:
            raise DocumentNotFoundError(f"Documento {document_id} nao encontrado.")
        logger.info("documento removido: %s", document_id)
