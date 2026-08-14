"""Ponto de entrada da API."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.__version__ import __version__
from app.api.routes import api_router, health_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import get_logger, setup_logging
from app.db.session import dispose_engine, init_db

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Recursos com ciclo de vida atrelado ao da aplicacao."""
    setup_logging()
    logger.info("iniciando %s (env=%s)", settings.PROJECT_NAME, settings.ENVIRONMENT)
    await init_db()
    yield
    await dispose_engine()
    logger.info("aplicacao finalizada")


def create_app() -> FastAPI:
    """Application factory — facilita testes e multiplas configuracoes."""
    app = FastAPI(
        title=settings.PROJECT_NAME,
        description=(
            "API de RAG sobre documentos: ingestao com chunking + embeddings "
            "em PostgreSQL/pgvector e perguntas em linguagem natural."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.ENVIRONMENT == "local" else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    return app


app = create_app()
