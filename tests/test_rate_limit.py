"""Testes dos limites de uso.

A chave de API diz quem pode chamar; estes limites dizem quanto. O que
se protege e a fatura do provedor: com a chave em maos, um laco de
`/ask` gasta a conta do dono sem que nenhum teste de autenticacao falhe.
"""

from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app.api.deps import get_document_service, get_rag_engine
from app.core.config import settings
from app.core.exceptions import RateLimitExceededError
from app.core.rate_limit import LimitadorJanelaFixa, identificar_cliente, reset_rate_limits
from app.core.security import API_KEY_HEADER_NAME
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

Configurador = Callable[..., None]


@pytest.fixture
def limites(monkeypatch: pytest.MonkeyPatch) -> Configurador:
    """Reconfigura os tetos e reconstroi os limitadores.

    A reconstrucao e obrigatoria: os limitadores leem as settings uma vez
    e vivem cacheados pelo processo.
    """

    def configurar(
        *,
        requisicoes: int = 1000,
        janela: int = 60,
        pagas: int = 1000,
        ligado: bool = True,
    ) -> None:
        monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", ligado)
        monkeypatch.setattr(settings, "RATE_LIMIT_REQUESTS", requisicoes)
        monkeypatch.setattr(settings, "RATE_LIMIT_WINDOW_SECONDS", janela)
        monkeypatch.setattr(settings, "RATE_LIMIT_METERED_DAILY", pagas)
        reset_rate_limits()

    return configurar


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Aplicacao com repositorio e providers falsos (sem banco, sem rede)."""
    monkeypatch.setattr(settings, "EMBEDDING_DIM", 8)
    repository = FakeDocumentRepository(matches=[make_match(page=3)])
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
async def api(app: FastAPI) -> Iterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


# ----------------------------------------------------------------------
# Janela curta (todas as rotas de documentos)
# ----------------------------------------------------------------------
async def test_janela_curta_recusa_com_429_e_retry_after(
    api: AsyncClient, limites: Configurador
) -> None:
    limites(requisicoes=2, janela=60)

    assert (await api.get("/api/v1/documents")).status_code == 200
    assert (await api.get("/api/v1/documents")).status_code == 200
    excedida = await api.get("/api/v1/documents")

    assert excedida.status_code == 429
    assert excedida.json()["error"] == RateLimitExceededError.__name__
    # Sem `Retry-After` o cliente educado nao tem quando voltar, e a
    # unica saida que sobra e tentar de novo em loop.
    assert int(excedida.headers["retry-after"]) > 0


async def test_health_fica_fora_do_limite(api: AsyncClient, limites: Configurador) -> None:
    """O monitor externo sonda de minuto em minuto e nao pode levar 429."""
    limites(requisicoes=1)

    for _ in range(5):
        assert (await api.get("/health")).status_code == 200


async def test_limite_desligado_nao_recusa(api: AsyncClient, limites: Configurador) -> None:
    """`RATE_LIMIT_ENABLED=false` e a valvula para diagnosticar em producao."""
    limites(requisicoes=1, ligado=False)

    for _ in range(5):
        assert (await api.get("/api/v1/documents")).status_code == 200


# ----------------------------------------------------------------------
# Teto diario (so as rotas que gastam tokens)
# ----------------------------------------------------------------------
async def test_teto_diario_recusa_a_segunda_pergunta(
    api: AsyncClient, limites: Configurador
) -> None:
    limites(pagas=1)

    primeira = await api.post("/api/v1/documents/ask", json={"question": "Qual o prazo?"})
    segunda = await api.post("/api/v1/documents/ask", json={"question": "E a multa?"})

    assert primeira.status_code == 200
    assert segunda.status_code == 429


async def test_upload_e_ask_dividem_o_mesmo_teto(api: AsyncClient, limites: Configurador) -> None:
    """O teto e de GASTO, e a fatura e uma so: os dois somam no mesmo balde."""
    limites(pagas=1)

    upload = await api.post(
        "/api/v1/documents/upload",
        files={"file": ("contrato.txt", b"Clausula primeira do contrato. " * 10, "text/plain")},
    )
    ask = await api.post("/api/v1/documents/ask", json={"question": "Qual o prazo?"})

    assert upload.status_code == 201
    assert ask.status_code == 429


async def test_rotas_de_leitura_nao_consomem_o_teto_diario(
    api: AsyncClient, limites: Configurador
) -> None:
    """Listar nao custa nada no provedor e nao pode gastar a cota da fatura."""
    limites(pagas=1)

    for _ in range(5):
        assert (await api.get("/api/v1/documents")).status_code == 200

    # A cota paga continua intacta depois das leituras.
    assert (await api.post("/api/v1/documents/ask", json={"question": "Qual?"})).status_code == 200


# ----------------------------------------------------------------------
# Limitador, isolado do HTTP
# ----------------------------------------------------------------------
def test_clientes_diferentes_tem_contadores_proprios() -> None:
    limitador = LimitadorJanelaFixa(limite=1, janela_segundos=60, escopo="teste")

    limitador.registrar("cliente-a")
    limitador.registrar("cliente-b")

    with pytest.raises(RateLimitExceededError):
        limitador.registrar("cliente-a")


def test_janela_zera_quando_expira(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passado o intervalo, o cliente recomeca do zero."""
    relogio = [1000.0]
    monkeypatch.setattr("app.core.rate_limit.time.monotonic", lambda: relogio[0])
    limitador = LimitadorJanelaFixa(limite=1, janela_segundos=60, escopo="teste")

    limitador.registrar("cliente")
    with pytest.raises(RateLimitExceededError):
        limitador.registrar("cliente")

    relogio[0] += 61
    limitador.registrar("cliente")  # nao levanta


def test_retry_after_diz_quanto_falta(monkeypatch: pytest.MonkeyPatch) -> None:
    relogio = [1000.0]
    monkeypatch.setattr("app.core.rate_limit.time.monotonic", lambda: relogio[0])
    limitador = LimitadorJanelaFixa(limite=1, janela_segundos=60, escopo="teste")

    limitador.registrar("cliente")
    relogio[0] += 20

    with pytest.raises(RateLimitExceededError) as excecao:
        limitador.registrar("cliente")

    assert excecao.value.retry_after == 40


# ----------------------------------------------------------------------
# Identidade do cliente
# ----------------------------------------------------------------------
def _request(headers: list[tuple[bytes, bytes]], client: tuple[str, int] | None) -> Request:
    return Request({"type": "http", "headers": headers, "client": client})


def test_identidade_prefere_a_chave_e_nao_a_expoe() -> None:
    """A identidade vai para log: nao pode carregar a credencial em claro."""
    chave = "chave-secreta-de-verdade"
    identidade = identificar_cliente(_request([(b"x-api-key", chave.encode())], ("1.2.3.4", 1)))

    assert identidade.startswith("chave:")
    assert chave not in identidade


def test_identidade_cai_no_ip_sem_chave() -> None:
    identidade = identificar_cliente(_request([], ("203.0.113.7", 1)))

    assert identidade == "ip:203.0.113.7"


def test_chave_nao_ascii_nao_quebra_a_identificacao() -> None:
    """Header chega decodificado como latin-1; `encode` nao pode explodir."""
    identidade = identificar_cliente(
        _request([(b"x-api-key", "chave-com-acento-ç".encode("latin-1"))], ("1.2.3.4", 1))
    )

    assert identidade.startswith("chave:")


def test_chaves_diferentes_nao_compartilham_cota() -> None:
    """Cliente identificado pela chave: um nao gasta a janela do outro."""
    limitador = LimitadorJanelaFixa(limite=1, janela_segundos=60, escopo="teste")
    primeira = identificar_cliente(_request([(API_KEY_HEADER_NAME.lower().encode(), b"aaa")], None))
    segunda = identificar_cliente(_request([(API_KEY_HEADER_NAME.lower().encode(), b"bbb")], None))

    limitador.registrar(primeira)
    limitador.registrar(segunda)  # nao levanta: identidades distintas

    assert primeira != segunda
