"""Ponto de entrada da API."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.__version__ import __version__
from app.api.routes import api_router, health_router
from app.core.body_limit import MULTIPART_OVERHEAD_BYTES, LimiteDeCorpoMiddleware
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import get_logger, setup_logging
from app.db.session import dispose_engine, init_db
from app.services.retention import (
    encerrar_rotina_de_retencao,
    iniciar_rotina_de_retencao,
)

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Recursos com ciclo de vida atrelado ao da aplicacao."""
    setup_logging()
    logger.info("iniciando %s (env=%s)", settings.PROJECT_NAME, settings.ENVIRONMENT)
    await init_db()
    # Depois do `init_db`: a primeira varredura acontece no boot, e varrer
    # antes de conferir o schema seria varrer um banco que pode nem ter as
    # tabelas.
    retencao = iniciar_rotina_de_retencao()
    yield
    await encerrar_rotina_de_retencao(retencao)
    await dispose_engine()
    logger.info("aplicacao finalizada")


def create_app() -> FastAPI:
    """Application factory — facilita testes e multiplas configuracoes."""
    # Fora de `local` a documentacao interativa sai do ar: ela descreve, para
    # quem passar na porta, exatamente como gastar a conta de API. Quem
    # integra recebe o OpenAPI por outro canal.
    expor_docs = settings.ENVIRONMENT == "local"

    app = FastAPI(
        title=settings.PROJECT_NAME,
        description=(
            "API de RAG sobre documentos: ingestao com chunking + embeddings "
            "em PostgreSQL/pgvector e perguntas em linguagem natural."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if expor_docs else None,
        redoc_url="/redoc" if expor_docs else None,
        openapi_url="/openapi.json" if expor_docs else None,
    )

    # A ordem de `add_middleware` e de dentro para fora: o ultimo
    # adicionado envolve os anteriores. O CORS vem por ultimo de proposito
    # — assim ate a recusa por corpo grande demais volta com os
    # cabecalhos de CORS, e o navegador mostra o 413 em vez de um erro
    # generico de rede.
    app.add_middleware(
        LimiteDeCorpoMiddleware,
        max_bytes=settings.MAX_UPLOAD_SIZE_BYTES + MULTIPART_OVERHEAD_BYTES,
    )

    # `allow_credentials=True` com origem "*" e recusado por todo navegador,
    # e aqui nao faz falta: a credencial e o header X-API-Key, nao um cookie.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if expor_docs else settings.CORS_ALLOW_ORIGINS,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    return app


app = create_app()
