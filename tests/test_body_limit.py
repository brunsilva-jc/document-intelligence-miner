"""Testes do teto de corpo da requisicao.

`DocumentProcessor.validate` ja devolve 413 para arquivo grande, mas so
depois de o multipart inteiro ter sido recebido e montado. Numa maquina
com `mem_limit`, receber para depois recusar e o proprio problema.
"""

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_document_service
from app.core.config import settings
from app.core.exceptions import FileTooLargeError
from app.main import create_app
from app.services.document_processor import DocumentProcessor
from app.services.document_service import DocumentService
from tests.factories import FakeDocumentRepository, FakeEmbeddingProvider

# Teto de 1 MB de arquivo: com a folga de multipart, o middleware corta
# em 2 MB. Numeros pequenos para o teste nao alocar dezenas de MB.
LIMITE_MB = 1
TETO_EFETIVO = 2 * 1024 * 1024


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(settings, "MAX_UPLOAD_SIZE_MB", LIMITE_MB)
    monkeypatch.setattr(settings, "EMBEDDING_DIM", 8)
    # `create_app` le o teto na construcao: a aplicacao tem de nascer
    # depois do monkeypatch.
    application = create_app()
    application.dependency_overrides[get_document_service] = lambda: DocumentService(
        FakeDocumentRepository(),
        processor=DocumentProcessor(chunk_size=120, chunk_overlap=20),
        embeddings=FakeEmbeddingProvider(dimension=8),
    )
    return application


@pytest.fixture
async def api(app: FastAPI) -> Iterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_content_length_acima_do_teto_e_recusado_sem_ler_o_corpo(
    api: AsyncClient,
) -> None:
    grande = b"x" * (TETO_EFETIVO + 1024)

    resposta = await api.post(
        "/api/v1/documents/upload",
        files={"file": ("gigante.txt", grande, "text/plain")},
    )

    assert resposta.status_code == 413
    assert resposta.json()["error"] == FileTooLargeError.__name__
    # `connection: close` so e escrito pela recusa do middleware — e o que
    # distingue esta resposta do 413 que a rota daria depois de ler tudo.
    assert resposta.headers.get("connection") == "close"


def _multipart_em_pedacos(
    pedaco: bytes, quantidade: int, contador: list[int]
) -> tuple[AsyncIterator[bytes], dict[str, str]]:
    """Monta um multipart valido enviado aos pedacos, sem `Content-Length`.

    Precisa ser multipart de verdade: com outro `Content-Type` o FastAPI
    recusa com 422 sem chegar a ler o corpo, e o contador nunca roda.
    """
    fronteira = "----teste-limite-de-corpo"
    cabecalho = (
        f"--{fronteira}\r\n"
        'Content-Disposition: form-data; name="file"; filename="gigante.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode()
    rodape = f"\r\n--{fronteira}--\r\n".encode()

    async def corpo() -> AsyncIterator[bytes]:
        yield cabecalho
        for _ in range(quantidade):
            contador[0] += 1
            yield pedaco
        yield rodape

    return corpo(), {"content-type": f"multipart/form-data; boundary={fronteira}"}


async def test_corpo_sem_content_length_e_abortado_no_meio(api: AsyncClient) -> None:
    """Corpo em `chunked` nao declara tamanho: so a contagem o segura.

    O teste conta quantos pedacos o cliente chegou a enviar: se o corte
    funcionou, o envio parou bem antes do fim.
    """
    total_de_pedacos = 40  # 10 MB, cinco vezes o teto
    enviados = [0]
    corpo, headers = _multipart_em_pedacos(b"y" * (256 * 1024), total_de_pedacos, enviados)

    resposta = await api.post("/api/v1/documents/upload", content=corpo, headers=headers)

    assert resposta.status_code == 413
    assert resposta.json()["error"] == FileTooLargeError.__name__
    assert enviados[0] < total_de_pedacos


async def test_content_length_malformado_cai_na_contagem(api: AsyncClient) -> None:
    """Tamanho ilegivel nao pode virar 500 nem passar batido."""
    enviados = [0]
    corpo, headers = _multipart_em_pedacos(b"z" * (256 * 1024), 40, enviados)
    headers["content-length"] = "nao-e-numero"

    resposta = await api.post("/api/v1/documents/upload", content=corpo, headers=headers)

    assert resposta.status_code == 413


async def test_upload_dentro_do_limite_continua_passando(api: AsyncClient) -> None:
    """O teto e a segunda linha: o caminho normal nao pode ser afetado."""
    conteudo = ". ".join(f"Clausula {i} do contrato" for i in range(20)).encode()

    resposta = await api.post(
        "/api/v1/documents/upload",
        files={"file": ("contrato.txt", conteudo, "text/plain")},
    )

    assert resposta.status_code == 201


async def test_arquivo_entre_o_teto_do_arquivo_e_o_do_corpo_leva_413_da_rota(
    api: AsyncClient,
) -> None:
    """Na folga de multipart quem recusa e a validacao, com a mensagem certa.

    O middleware e grosseiro de proposito (fala de corpo); a mensagem que
    fala de arquivo, com o limite em MB, vem de `DocumentProcessor`.
    """
    # Acima de 1 MB (limite do arquivo) e abaixo de 2 MB (teto do corpo).
    conteudo = b"w" * (1_500_000)

    resposta = await api.post(
        "/api/v1/documents/upload",
        files={"file": ("medio.txt", conteudo, "text/plain")},
    )

    assert resposta.status_code == 413
    assert "MB" in resposta.json()["detail"]
    # Veio da rota, nao do middleware.
    assert resposta.headers.get("connection") != "close"
