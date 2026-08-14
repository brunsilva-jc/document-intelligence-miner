"""Testes do motor de RAG (recuperacao + montagem do prompt)."""

import pytest

from app.core.config import settings
from app.core.exceptions import NoRelevantContextError
from app.services.rag_engine import RagEngine
from tests.factories import (
    FakeDocumentRepository,
    FakeEmbeddingProvider,
    FakeLLMProvider,
    make_match,
)


def build_engine(matches=None) -> tuple[RagEngine, FakeDocumentRepository, FakeLLMProvider]:
    repository = FakeDocumentRepository(matches=matches or [])
    llm = FakeLLMProvider()
    engine = RagEngine(repository, FakeEmbeddingProvider(), llm)
    return engine, repository, llm


async def test_answer_returns_sources_and_model() -> None:
    matches = [make_match("Prazo de 24 meses.", page=3), make_match("Multa de 10%.", page=7)]
    engine, _, llm = build_engine(matches)

    result = await engine.answer("Qual o prazo?")

    assert result.answer == llm.answer
    assert result.model == "fake-llm"
    assert len(result.matches) == 2
    assert result.elapsed_ms >= 0


async def test_prompt_carries_numbered_context_with_citable_sources() -> None:
    matches = [make_match("Prazo de 24 meses.", filename="contrato.pdf", page=3)]
    engine, _, llm = build_engine(matches)

    await engine.answer("Qual o prazo?")

    assert "[1] (fonte: contrato.pdf, pagina 3)" in llm.user_prompt
    assert "Prazo de 24 meses." in llm.user_prompt
    assert "Qual o prazo?" in llm.user_prompt


async def test_system_prompt_forbids_outside_knowledge() -> None:
    engine, _, llm = build_engine([make_match()])

    await engine.answer("Qual o prazo?")

    assert "EXCLUSIVAMENTE" in llm.system_prompt
    assert "Nao encontrei essa informacao nos documentos fornecidos." in llm.system_prompt


async def test_raises_when_nothing_relevant_is_found() -> None:
    engine, _, llm = build_engine([])

    with pytest.raises(NoRelevantContextError):
        await engine.answer("Pergunta sobre assunto ausente")

    # O LLM nao pode ser chamado sem contexto: convite a alucinacao.
    assert llm.user_prompt is None


async def test_retrieval_uses_configured_defaults() -> None:
    engine, repository, _ = build_engine([make_match()])

    await engine.answer("Qual o prazo?")

    call = repository.search_calls[0]
    assert call["top_k"] == settings.RETRIEVAL_TOP_K
    assert call["max_distance"] == settings.RETRIEVAL_MAX_DISTANCE


async def test_explicit_top_k_and_document_filter_are_forwarded() -> None:
    matches = [make_match(f"trecho {i}") for i in range(5)]
    engine, repository, _ = build_engine(matches)
    document_ids = [matches[0].chunk.document_id]

    result = await engine.answer("Qual o prazo?", top_k=2, document_ids=document_ids)

    assert repository.search_calls[0]["top_k"] == 2
    assert repository.search_calls[0]["document_ids"] == document_ids
    assert len(result.matches) == 2


def test_score_is_the_complement_of_cosine_distance() -> None:
    assert make_match(distance=0.0).score == pytest.approx(1.0)
    assert make_match(distance=0.25).score == pytest.approx(0.75)
