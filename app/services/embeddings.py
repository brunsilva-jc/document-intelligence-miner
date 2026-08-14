"""Geracao de embeddings, isolada atras de um Protocol.

O resto da aplicacao depende de `EmbeddingProvider`, nunca de OpenAI ou
HuggingFace diretamente: trocar de provedor (ou usar um fake nos testes)
nao toca em service, repositorio ou rota.
"""

from functools import lru_cache
from typing import Protocol, runtime_checkable

import anyio

from app.core.config import settings
from app.core.exceptions import EmbeddingProviderError
from app.core.logging import get_logger

logger = get_logger(__name__)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Contrato minimo esperado de um provedor de embeddings."""

    model_name: str
    dimension: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Vetoriza um lote de textos (ingestao)."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """Vetoriza uma pergunta (busca)."""
        ...


class OpenAIEmbeddingProvider:
    """Embeddings via API da OpenAI (nativamente assincrona)."""

    def __init__(self, model: str, dimension: int, api_key: str) -> None:
        from langchain_openai import OpenAIEmbeddings

        self.model_name = model
        self.dimension = dimension
        # `dimensions` so e aceito pelos modelos text-embedding-3-*.
        kwargs: dict = {"model": model, "api_key": api_key}
        if model.startswith("text-embedding-3"):
            kwargs["dimensions"] = dimension
        self._client = OpenAIEmbeddings(**kwargs)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        try:
            return await self._client.aembed_documents(texts)
        except Exception as exc:
            logger.exception("falha ao vetorizar lote de %d textos", len(texts))
            raise EmbeddingProviderError(f"Erro no provedor de embeddings: {exc}") from exc

    async def embed_query(self, text: str) -> list[float]:
        try:
            return await self._client.aembed_query(text)
        except Exception as exc:
            logger.exception("falha ao vetorizar consulta")
            raise EmbeddingProviderError(f"Erro no provedor de embeddings: {exc}") from exc


class HuggingFaceEmbeddingProvider:
    """Embeddings locais via sentence-transformers.

    A biblioteca e sincrona e usa CPU/GPU intensivamente, entao cada
    chamada roda em worker thread para nao travar o event loop.
    """

    def __init__(self, model: str, dimension: int) -> None:
        try:
            from langchain_huggingface import HuggingFaceEmbeddings
        except ImportError as exc:  # pragma: no cover - depende do extra
            raise EmbeddingProviderError(
                "EMBEDDING_PROVIDER=huggingface exige `pip install -r requirements-ml.txt`."
            ) from exc

        self.model_name = model
        self.dimension = dimension
        self._client = HuggingFaceEmbeddings(model_name=model)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        try:
            return await anyio.to_thread.run_sync(self._client.embed_documents, texts)
        except Exception as exc:
            logger.exception("falha ao vetorizar lote de %d textos", len(texts))
            raise EmbeddingProviderError(f"Erro no provedor de embeddings: {exc}") from exc

    async def embed_query(self, text: str) -> list[float]:
        try:
            return await anyio.to_thread.run_sync(self._client.embed_query, text)
        except Exception as exc:
            logger.exception("falha ao vetorizar consulta")
            raise EmbeddingProviderError(f"Erro no provedor de embeddings: {exc}") from exc


@lru_cache(maxsize=1)
def get_embedding_provider() -> EmbeddingProvider:
    """Instancia unica do provedor configurado.

    Cacheada porque o backend HuggingFace carrega o modelo em memoria —
    reinstanciar por request custaria segundos e centenas de MB.
    """
    if settings.EMBEDDING_PROVIDER == "openai":
        if not settings.OPENAI_API_KEY:
            raise EmbeddingProviderError(
                "OPENAI_API_KEY nao configurada (EMBEDDING_PROVIDER=openai)."
            )
        provider: EmbeddingProvider = OpenAIEmbeddingProvider(
            model=settings.EMBEDDING_MODEL,
            dimension=settings.EMBEDDING_DIM,
            api_key=settings.OPENAI_API_KEY,
        )
    else:
        provider = HuggingFaceEmbeddingProvider(
            model=settings.EMBEDDING_MODEL,
            dimension=settings.EMBEDDING_DIM,
        )

    logger.info(
        "provedor de embeddings: %s (%s, dim=%d)",
        settings.EMBEDDING_PROVIDER,
        provider.model_name,
        provider.dimension,
    )
    return provider


def validate_dimension(vectors: list[list[float]], expected: int) -> None:
    """Falha cedo se o modelo devolver dimensao diferente da coluna.

    Sem isso, o erro so apareceria no INSERT, como uma mensagem opaca do
    PostgreSQL — e depois de ja termos pago pela chamada de API.
    """
    for vector in vectors:
        if len(vector) != expected:
            raise EmbeddingProviderError(
                f"Modelo retornou embedding de dimensao {len(vector)}, "
                f"mas o schema espera {expected}. Ajuste EMBEDDING_DIM e reprocesse o acervo."
            )
