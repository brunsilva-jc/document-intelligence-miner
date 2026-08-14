"""Motor de RAG: recupera contexto e monta a resposta com o LLM.

Fluxo: pergunta -> embedding -> busca por cosseno no pgvector -> top-k
blocos -> prompt com o contexto -> LLM -> resposta com citacoes.
"""

import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol, runtime_checkable

from app.core.config import settings
from app.core.exceptions import LLMProviderError, NoRelevantContextError
from app.core.logging import get_logger
from app.repositories.document_repository import ChunkMatch, DocumentRepository
from app.services.embeddings import EmbeddingProvider

logger = get_logger(__name__)

SYSTEM_PROMPT = """Voce e um assistente de analise documental. Responda a \
pergunta do usuario usando EXCLUSIVAMENTE o contexto fornecido.

Regras:
- Nunca use conhecimento externo ao contexto, mesmo que voce saiba a resposta.
- Se o contexto nao contiver a informacao, diga exatamente: "Nao encontrei \
essa informacao nos documentos fornecidos." e nada mais.
- Cite as fontes usadas com o numero do trecho entre colchetes, ex: [1], [2].
- Responda no mesmo idioma da pergunta, de forma direta e objetiva.
- Nao invente numeros, datas, nomes ou clausulas que nao estejam no contexto."""

USER_PROMPT = """Contexto:
{context}

Pergunta: {question}

Resposta:"""


@runtime_checkable
class LLMProvider(Protocol):
    """Contrato do gerador de texto (mockavel nos testes)."""

    model_name: str

    async def generate(self, system_prompt: str, user_prompt: str) -> str: ...


class OpenAIChatProvider:
    """Geracao via Chat Completions da OpenAI."""

    def __init__(self, model: str, temperature: float, api_key: str) -> None:
        from langchain_openai import ChatOpenAI

        self.model_name = model
        self._client = ChatOpenAI(model=model, temperature=temperature, api_key=api_key)

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        try:
            response = await self._client.ainvoke(
                [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
            )
        except Exception as exc:
            logger.exception("falha na chamada ao LLM")
            raise LLMProviderError(f"Erro no provedor de LLM: {exc}") from exc

        content = response.content
        # Modelos multimodais podem devolver uma lista de blocos.
        if isinstance(content, list):
            content = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        return str(content).strip()


@lru_cache(maxsize=1)
def get_llm_provider() -> LLMProvider:
    """Instancia unica do LLM configurado."""
    if not settings.OPENAI_API_KEY:
        raise LLMProviderError("OPENAI_API_KEY nao configurada.")

    provider: LLMProvider = OpenAIChatProvider(
        model=settings.LLM_MODEL,
        temperature=settings.LLM_TEMPERATURE,
        api_key=settings.OPENAI_API_KEY,
    )
    logger.info("provedor de LLM: %s", provider.model_name)
    return provider


@dataclass(slots=True)
class RagAnswer:
    """Resultado de uma consulta RAG, antes de virar schema de resposta."""

    question: str
    answer: str
    matches: list[ChunkMatch]
    model: str
    elapsed_ms: int


class RagEngine:
    """Orquestra recuperacao e geracao."""

    def __init__(
        self,
        repository: DocumentRepository,
        embeddings: EmbeddingProvider,
        llm: LLMProvider,
    ) -> None:
        self._repository = repository
        self._embeddings = embeddings
        self._llm = llm

    async def answer(
        self,
        question: str,
        *,
        top_k: int | None = None,
        document_ids: list[uuid.UUID] | None = None,
    ) -> RagAnswer:
        started = time.perf_counter()
        top_k = top_k or settings.RETRIEVAL_TOP_K

        matches = await self.retrieve(question, top_k=top_k, document_ids=document_ids)
        context = self._build_context(matches)

        answer = await self._llm.generate(
            SYSTEM_PROMPT,
            USER_PROMPT.format(context=context, question=question.strip()),
        )

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "pergunta respondida em %dms com %d trechos (melhor score=%.3f)",
            elapsed_ms,
            len(matches),
            matches[0].score,
        )
        return RagAnswer(
            question=question,
            answer=answer,
            matches=matches,
            model=self._llm.model_name,
            elapsed_ms=elapsed_ms,
        )

    async def retrieve(
        self,
        question: str,
        *,
        top_k: int,
        document_ids: list[uuid.UUID] | None = None,
    ) -> list[ChunkMatch]:
        """Busca os blocos mais relevantes para a pergunta."""
        query_vector = await self._embeddings.embed_query(question.strip())

        matches = await self._repository.search_similar_chunks(
            query_vector,
            top_k=top_k,
            max_distance=settings.RETRIEVAL_MAX_DISTANCE,
            document_ids=document_ids,
        )

        if not matches:
            # Sem contexto, chamar o LLM so convida a alucinacao — e melhor
            # dizer que nao sabemos do que pagar por uma resposta inventada.
            raise NoRelevantContextError(
                "Nenhum trecho relevante encontrado. Verifique se ha documentos "
                "ingeridos ou reformule a pergunta."
            )
        return matches

    @staticmethod
    def _build_context(matches: list[ChunkMatch]) -> str:
        """Numera os trechos para que o LLM possa cita-los."""
        blocks = []
        for position, match in enumerate(matches, start=1):
            page = match.chunk.chunk_metadata.get("page")
            origin = f"{match.filename}, pagina {page}" if page else match.filename
            blocks.append(f"[{position}] (fonte: {origin})\n{match.chunk.content}")
        return "\n\n---\n\n".join(blocks)
