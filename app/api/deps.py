"""Dependencias compartilhadas pelas rotas (injecao de dependencia)."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.repositories.document_repository import DocumentRepository
from app.services.document_processor import DocumentProcessor
from app.services.document_service import DocumentService
from app.services.embeddings import EmbeddingProvider, get_embedding_provider
from app.services.rag_engine import LLMProvider, RagEngine, get_llm_provider

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_document_repository(session: SessionDep) -> DocumentRepository:
    return DocumentRepository(session)


RepositoryDep = Annotated[DocumentRepository, Depends(get_document_repository)]

# Providers de IA sao cacheados (`lru_cache`) nos proprios modulos: um
# unico cliente/modelo vive pelo processo inteiro, nao por request.
EmbeddingsDep = Annotated[EmbeddingProvider, Depends(get_embedding_provider)]
LLMDep = Annotated[LLMProvider, Depends(get_llm_provider)]


def get_document_processor() -> DocumentProcessor:
    return DocumentProcessor()


ProcessorDep = Annotated[DocumentProcessor, Depends(get_document_processor)]


def get_document_service(
    repository: RepositoryDep,
    processor: ProcessorDep,
) -> DocumentService:
    # O provedor de embeddings e resolvido sob demanda pelo service: as
    # rotas de leitura nao devem exigir chave de API para funcionar.
    return DocumentService(repository, processor=processor)


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]


def get_rag_engine(
    repository: RepositoryDep,
    embeddings: EmbeddingsDep,
    llm: LLMDep,
) -> RagEngine:
    return RagEngine(repository, embeddings, llm)


RagEngineDep = Annotated[RagEngine, Depends(get_rag_engine)]
