"""Testes das rotas /documents com as dependencias sobrescritas.

Exercitam serializacao, codigos de status e traducao de erros de dominio
sem PostgreSQL nem chave de API.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_document_service, get_rag_engine
from app.core.exceptions import NoRelevantContextError
from app.main import create_app
from app.services.document_processor import DocumentProcessor
from app.services.document_service import DocumentService
from app.services.rag_engine import RagEngine
from tests.factories import (
    FakeDocumentRepository,
    FakeEmbeddingProvider,
    FakeLLMProvider,
    make_match,
)

TEXT = ". ".join(f"Clausula {i} do contrato" for i in range(20)).encode()


@pytest.fixture
def repository() -> FakeDocumentRepository:
    return FakeDocumentRepository(matches=[make_match(page=3)])


@pytest.fixture
def app(repository: FakeDocumentRepository, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from app.core.config import settings

    monkeypatch.setattr(settings, "EMBEDDING_DIM", 8)
    application = create_app()

    application.dependency_overrides[get_document_service] = lambda: DocumentService(
        repository,
        processor=DocumentProcessor(chunk_size=120, chunk_overlap=20),
        embeddings=FakeEmbeddingProvider(dimension=8),
    )
    application.dependency_overrides[get_rag_engine] = lambda: RagEngine(
        repository, FakeEmbeddingProvider(dimension=8), FakeLLMProvider()
    )
    return application


@pytest.fixture
async def api(app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


# ----------------------------------------------------------------------
# Upload
# ----------------------------------------------------------------------
async def test_upload_returns_201_with_chunk_count(api: AsyncClient) -> None:
    response = await api.post(
        "/api/v1/documents/upload",
        files={"file": ("contrato.txt", TEXT, "text/plain")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["chunks_created"] > 0
    assert body["duplicated"] is False
    assert body["document"]["filename"] == "contrato.txt"
    assert body["document"]["status"] == "completed"


async def test_upload_same_file_twice_is_idempotent(api: AsyncClient) -> None:
    files = {"file": ("contrato.txt", TEXT, "text/plain")}
    first = await api.post("/api/v1/documents/upload", files=files)
    second = await api.post("/api/v1/documents/upload", files=files)

    assert second.status_code == 201
    assert second.json()["duplicated"] is True
    assert second.json()["document"]["id"] == first.json()["document"]["id"]


async def test_upload_rejects_unsupported_type_with_415(api: AsyncClient) -> None:
    response = await api.post(
        "/api/v1/documents/upload",
        files={"file": ("planilha.xlsx", b"binario", "application/vnd.ms-excel")},
    )

    assert response.status_code == 415
    assert response.json()["error"] == "UnsupportedFileTypeError"


async def test_upload_rejects_empty_file(api: AsyncClient) -> None:
    response = await api.post(
        "/api/v1/documents/upload", files={"file": ("vazio.txt", b"", "text/plain")}
    )

    assert response.status_code == 422


# ----------------------------------------------------------------------
# Ask
# ----------------------------------------------------------------------
async def test_ask_returns_answer_with_cited_sources(api: AsyncClient) -> None:
    response = await api.post(
        "/api/v1/documents/ask", json={"question": "Qual o prazo de vigencia?"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Resposta gerada. [1]"
    assert body["model"] == "fake-llm"
    assert len(body["sources"]) == 1

    source = body["sources"][0]
    assert source["page"] == 3
    assert source["score"] == pytest.approx(0.88)
    assert source["filename"] == "contrato.pdf"


async def test_ask_without_relevant_context_returns_404(app: FastAPI, api: AsyncClient) -> None:
    empty_repository = FakeDocumentRepository(matches=[])
    app.dependency_overrides[get_rag_engine] = lambda: RagEngine(
        empty_repository, FakeEmbeddingProvider(dimension=8), FakeLLMProvider()
    )

    response = await api.post(
        "/api/v1/documents/ask", json={"question": "Assunto inexistente no acervo"}
    )

    assert response.status_code == 404
    assert response.json()["error"] == NoRelevantContextError.__name__


async def test_ask_validates_question_length(api: AsyncClient) -> None:
    response = await api.post("/api/v1/documents/ask", json={"question": "a"})

    assert response.status_code == 422


# ----------------------------------------------------------------------
# Listagem e remocao
# ----------------------------------------------------------------------
async def test_list_and_delete_document(api: AsyncClient) -> None:
    upload = await api.post(
        "/api/v1/documents/upload",
        files={"file": ("contrato.txt", TEXT, "text/plain")},
    )
    document_id = upload.json()["document"]["id"]

    listing = await api.get("/api/v1/documents")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1

    deletion = await api.delete(f"/api/v1/documents/{document_id}")
    assert deletion.status_code == 204

    assert (await api.get("/api/v1/documents")).json()["total"] == 0
    assert (await api.get(f"/api/v1/documents/{document_id}")).status_code == 404
