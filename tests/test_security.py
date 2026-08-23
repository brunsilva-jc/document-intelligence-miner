"""Testes da chave de API.

O que se protege aqui e a conta do provedor de LLM: `/upload` e `/ask`
gastam dinheiro por requisicao. Um 401 que vira 200 por descuido nao
aparece em nenhum grafico ate a fatura chegar.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.security import API_KEY_HEADER_NAME
from app.main import create_app

CHAVE = "chave-de-teste-com-tamanho-mais-que-suficiente"


@pytest.fixture
def com_chave(monkeypatch: pytest.MonkeyPatch) -> str:
    """Liga a exigencia de chave na instancia de Settings em uso."""
    monkeypatch.setattr(settings, "API_KEY", CHAVE)
    return CHAVE


@pytest.fixture
async def client_protegido(com_chave: str):
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_documentos_sem_chave_retorna_401(client_protegido: AsyncClient) -> None:
    response = await client_protegido.post("/api/v1/documents/ask", json={"question": "oi"})

    assert response.status_code == 401
    body = response.json()
    assert body["error"] == "InvalidApiKeyError"


async def test_documentos_com_chave_errada_retorna_401(client_protegido: AsyncClient) -> None:
    response = await client_protegido.post(
        "/api/v1/documents/ask",
        json={"question": "oi"},
        headers={API_KEY_HEADER_NAME: "chave-errada-do-mesmo-tamanho-que-a-certa"},
    )

    assert response.status_code == 401


async def test_chave_nao_ascii_e_recusada_sem_explodir(client_protegido: AsyncClient) -> None:
    """`secrets.compare_digest` com `str` levanta TypeError fora do ASCII.

    O contrato e 401 (credencial invalida), nunca 500.
    """
    # Em bytes porque o httpx recusa `str` nao-ASCII antes de enviar; o
    # servidor decodifica header como latin-1, entao esse byte chega mesmo.
    response = await client_protegido.get(
        "/api/v1/documents",
        headers={API_KEY_HEADER_NAME: "chave-com-acento-invalida-ç".encode("latin-1")},
    )

    assert response.status_code == 401


async def test_listagem_tambem_e_protegida(client_protegido: AsyncClient) -> None:
    """A protecao esta no router: vale para as rotas de leitura tambem."""
    response = await client_protegido.get("/api/v1/documents")

    assert response.status_code == 401


async def test_chave_correta_passa_da_autenticacao(client_protegido: AsyncClient) -> None:
    """Com a chave certa a requisicao segue e falha adiante, no provedor.

    502 aqui e sucesso do ponto de vista da autenticacao: significa que a
    rota rodou e parou por falta de OPENAI_API_KEY, nao por credencial.
    """
    response = await client_protegido.post(
        "/api/v1/documents/ask",
        json={"question": "oi"},
        headers={API_KEY_HEADER_NAME: CHAVE},
    )

    assert response.status_code == 502


async def test_health_fica_aberto_mesmo_com_chave(client_protegido: AsyncClient) -> None:
    """O monitor externo nao tem credencial."""
    response = await client_protegido.get("/health")

    assert response.status_code == 200


async def test_health_responde_a_HEAD(client_protegido: AsyncClient) -> None:
    """UptimeRobot sonda com HEAD por padrao.

    Um /health que so aceita GET responde 405 e o monitor passa a acusar
    queda com o servico de pe — ja aconteceu no orchestrator.
    """
    response = await client_protegido.head("/health")

    assert response.status_code == 200


async def test_sem_chave_configurada_a_checagem_fica_desligada(client: AsyncClient) -> None:
    """Fixture `client` roda com API_KEY vazia (ENVIRONMENT=local).

    Sem isso todo `docker compose up` de desenvolvimento passaria a exigir
    header — e o custo de atrito viraria motivo para desligar a protecao.
    """
    response = await client.post("/api/v1/documents/ask", json={"question": "oi"})

    assert response.status_code == 502
