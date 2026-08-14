"""Testes do pipeline de ingestao."""

import pytest

from app.core.config import settings
from app.core.exceptions import EmbeddingProviderError, UnsupportedFileTypeError
from app.models.domain import DocumentStatus
from app.services.document_processor import DocumentProcessor
from app.services.document_service import DocumentService
from tests.factories import FakeDocumentRepository, FakeEmbeddingProvider

TEXT = ". ".join(f"Clausula {i} do contrato de prestacao de servicos" for i in range(30))


def build_service(dimension: int = 8) -> tuple[DocumentService, FakeDocumentRepository]:
    repository = FakeDocumentRepository()
    service = DocumentService(
        repository,
        processor=DocumentProcessor(chunk_size=120, chunk_overlap=20),
        embeddings=FakeEmbeddingProvider(dimension=dimension),
    )
    return service, repository


@pytest.fixture(autouse=True)
def _match_embedding_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alinha a dimensao esperada com a do provedor falso."""
    monkeypatch.setattr(settings, "EMBEDDING_DIM", 8)


async def test_ingest_persists_document_and_chunks() -> None:
    service, repository = build_service()

    result = await service.ingest(
        filename="contrato.txt", content_type="text/plain", data=TEXT.encode()
    )

    assert result.duplicated is False
    assert result.chunks_created == len(repository.chunks) > 1
    assert result.document.status == DocumentStatus.COMPLETED
    assert result.document.chunk_count == result.chunks_created


async def test_ingest_stores_embeddings_and_positions() -> None:
    service, repository = build_service()

    await service.ingest(filename="contrato.txt", content_type="text/plain", data=TEXT.encode())

    indexes = [chunk.chunk_index for chunk in repository.chunks]
    assert indexes == list(range(len(indexes)))
    assert all(len(chunk.embedding) == 8 for chunk in repository.chunks)
    assert all(chunk.content for chunk in repository.chunks)


async def test_ingest_records_provenance_metadata() -> None:
    service, _ = build_service()

    result = await service.ingest(
        filename="contrato.txt", content_type="text/plain", data=TEXT.encode()
    )

    metadata = result.document.doc_metadata
    assert metadata["embedding_model"] == "fake-embedding"
    assert metadata["pages"] == 1
    assert metadata["characters"] == len(TEXT)


async def test_duplicate_upload_is_detected_by_checksum() -> None:
    service, repository = build_service()
    payload = TEXT.encode()

    first = await service.ingest(filename="contrato.txt", content_type="text/plain", data=payload)
    second = await service.ingest(
        filename="copia-do-contrato.txt", content_type="text/plain", data=payload
    )

    assert second.duplicated is True
    assert second.document.id == first.document.id
    assert second.chunks_created == first.chunks_created
    # Nada foi reprocessado nem regravado.
    assert len(repository.documents) == 1
    assert len(repository.chunks) == first.chunks_created


async def test_invalid_file_is_rejected_before_any_write() -> None:
    service, repository = build_service()

    with pytest.raises(UnsupportedFileTypeError):
        await service.ingest(
            filename="planilha.xlsx", content_type="application/vnd.ms-excel", data=b"x"
        )

    assert repository.documents == {}


async def test_dimension_mismatch_fails_before_touching_the_database() -> None:
    # Provedor devolve 16 dimensoes, mas a coluna espera 8.
    service, repository = build_service(dimension=16)

    with pytest.raises(EmbeddingProviderError, match="dimensao"):
        await service.ingest(filename="contrato.txt", content_type="text/plain", data=TEXT.encode())

    assert repository.documents == {}
    assert repository.chunks == []


async def test_embeddings_are_requested_in_batches() -> None:
    repository = FakeDocumentRepository()
    embeddings = FakeEmbeddingProvider(dimension=8)
    service = DocumentService(
        repository,
        processor=DocumentProcessor(chunk_size=60, chunk_overlap=0),
        embeddings=embeddings,
    )

    await service.ingest(filename="contrato.txt", content_type="text/plain", data=TEXT.encode())

    total_texts = sum(len(batch) for batch in embeddings.calls)
    assert total_texts == len(repository.chunks)
    assert all(len(batch) <= 64 for batch in embeddings.calls)
