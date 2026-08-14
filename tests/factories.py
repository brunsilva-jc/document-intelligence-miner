"""Dublês de teste: providers e repositorio falsos, sem rede nem banco.

Manter os fakes aqui e o que permite testar service, engine e rotas sem
PostgreSQL e sem chave de API — requisito para o CI rodar em segundos.
"""

import uuid
from datetime import UTC, datetime

from app.models.domain import Document, DocumentChunk, DocumentStatus
from app.repositories.document_repository import ChunkMatch


class FakeEmbeddingProvider:
    """Embeddings deterministicos: hash do texto espalhado no vetor."""

    def __init__(self, dimension: int = 8, model_name: str = "fake-embedding") -> None:
        self.dimension = dimension
        self.model_name = model_name
        self.calls: list[list[str]] = []

    def _vector(self, text: str) -> list[float]:
        seed = abs(hash(text))
        return [((seed >> (i * 3)) % 100) / 100 for i in range(self.dimension)]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class FakeLLMProvider:
    """LLM que devolve uma resposta fixa e guarda o prompt recebido."""

    def __init__(self, answer: str = "Resposta gerada. [1]") -> None:
        self.model_name = "fake-llm"
        self.answer = answer
        self.system_prompt: str | None = None
        self.user_prompt: str | None = None

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return self.answer


class FakeDocumentRepository:
    """Implementacao em memoria da interface do repositorio."""

    def __init__(self, matches: list[ChunkMatch] | None = None) -> None:
        self.documents: dict[uuid.UUID, Document] = {}
        self.chunks: list[DocumentChunk] = []
        self.matches = matches or []
        self.search_calls: list[dict] = []

    async def create(
        self,
        *,
        filename: str,
        content_type: str,
        size_bytes: int,
        checksum: str,
        metadata: dict | None = None,
    ) -> Document:
        document = make_document(
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            checksum=checksum,
            metadata=metadata or {},
        )
        self.documents[document.id] = document
        return document

    async def get_by_id(self, document_id: uuid.UUID) -> Document | None:
        return self.documents.get(document_id)

    async def get_by_checksum(self, checksum: str) -> Document | None:
        return next((doc for doc in self.documents.values() if doc.checksum == checksum), None)

    async def list_all(self, *, limit: int = 50, offset: int = 0) -> list[Document]:
        return list(self.documents.values())[offset : offset + limit]

    async def count(self) -> int:
        return len(self.documents)

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
        return document

    async def delete(self, document_id: uuid.UUID) -> bool:
        return self.documents.pop(document_id, None) is not None

    async def add_chunks(self, chunks) -> int:
        self.chunks.extend(chunks)
        return len(chunks)

    async def count_chunks(self, document_id: uuid.UUID) -> int:
        return sum(1 for chunk in self.chunks if chunk.document_id == document_id)

    async def search_similar_chunks(
        self,
        embedding,
        *,
        top_k: int,
        max_distance: float | None = None,
        document_ids=None,
    ) -> list[ChunkMatch]:
        self.search_calls.append(
            {"top_k": top_k, "max_distance": max_distance, "document_ids": document_ids}
        )
        return self.matches[:top_k]


def make_document(
    *,
    filename: str = "contrato.pdf",
    content_type: str = "application/pdf",
    size_bytes: int = 1024,
    checksum: str = "abc123",
    status: DocumentStatus = DocumentStatus.COMPLETED,
    chunk_count: int = 0,
    metadata: dict | None = None,
) -> Document:
    """Document ORM valido fora de sessao (timestamps preenchidos na mao)."""
    document = Document(
        id=uuid.uuid4(),
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        checksum=checksum,
        status=status,
        chunk_count=chunk_count,
        doc_metadata=metadata or {},
    )
    now = datetime.now(UTC)
    document.created_at = now
    document.updated_at = now
    return document


def make_match(
    content: str = "O prazo de vigencia e de 24 meses.",
    *,
    filename: str = "contrato.pdf",
    distance: float = 0.12,
    page: int = 3,
    chunk_index: int = 0,
) -> ChunkMatch:
    chunk = DocumentChunk(
        id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        chunk_index=chunk_index,
        content=content,
        token_count=len(content.split()),
        embedding=[0.0] * 8,
        chunk_metadata={"page": page},
    )
    return ChunkMatch(chunk=chunk, filename=filename, distance=distance)
