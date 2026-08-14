"""Testes de fumaca da aplicacao (sem dependencia de banco)."""

from httpx import AsyncClient

from app.__version__ import __version__


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__


async def test_openapi_schema_is_generated(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/documents/upload" in paths
    assert "/api/v1/documents/ask" in paths


async def test_ask_without_api_key_fails_as_bad_gateway(client: AsyncClient) -> None:
    """Sem OPENAI_API_KEY o provedor falha ao resolver a dependencia.

    O contrato aqui e o codigo de status: falha de provedor externo vira
    502, nao 500 — o cliente sabe que o problema nao e a requisicao dele.
    """
    response = await client.post("/api/v1/documents/ask", json={"question": "Qual o prazo?"})

    assert response.status_code == 502
    body = response.json()
    assert body["error"] in {"EmbeddingProviderError", "LLMProviderError"}
    assert "OPENAI_API_KEY" in body["detail"]
