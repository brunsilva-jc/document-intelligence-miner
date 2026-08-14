"""Engine, sessao e bootstrap do PostgreSQL + pgvector."""

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DB_ECHO,
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependencia do FastAPI: unit of work por request.

    Commit ao final de um request bem-sucedido, rollback em qualquer
    excecao. Services que precisam de transacoes menores podem chamar
    `session.commit()` explicitamente (ex.: marcar status de ingestao).
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Garante a extensao pgvector e cria o schema.

    Conveniencia para o ambiente local. Em staging/producao o schema deve
    ser versionado com Alembic (`alembic upgrade head`) — veja o README.
    """
    # Import tardio: registra os modelos no metadata da Base.
    from app.db.base import Base
    from app.models import domain  # noqa: F401

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        if settings.ENVIRONMENT == "local":
            await conn.run_sync(Base.metadata.create_all)
            logger.info("schema sincronizado via metadata.create_all")


async def check_db_connection() -> bool:
    """Ping usado pelo endpoint de readiness."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover - depende de infra
        logger.warning("banco indisponivel: %s", exc)
        return False


async def dispose_engine() -> None:
    """Fecha o pool de conexoes no shutdown."""
    await engine.dispose()
