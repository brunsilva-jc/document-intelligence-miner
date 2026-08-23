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
    """Garante a extensao pgvector e confere o schema.

    Em `local` o schema nasce do proprio metadata — conveniencia para quem
    acabou de clonar o repositorio. Fora de `local` quem cria o schema e o
    Alembic (`alembic upgrade head`), e o papel desta funcao passa a ser
    CONFERIR: uma API que sobe com as tabelas faltando parece saudavel no
    /health e responde 500 em tudo que importa. Melhor nao subir.
    """
    # Import tardio: registra os modelos no metadata da Base.
    from app.db.base import Base
    from app.models import domain  # noqa: F401

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

        if settings.ENVIRONMENT == "local":
            await conn.run_sync(Base.metadata.create_all)
            logger.info("schema sincronizado via metadata.create_all (so em local)")
            return

        # `to_regclass` devolve NULL em vez de levantar erro quando a
        # tabela nao existe — da para perguntar sem abortar a transacao.
        schema_aplicado = await conn.scalar(text("SELECT to_regclass('public.document_chunks')"))
        if schema_aplicado is None:
            raise RuntimeError(
                "schema ausente no banco: rode `alembic upgrade head` antes de subir a API "
                f"(ENVIRONMENT={settings.ENVIRONMENT})"
            )

        revisao = None
        if await conn.scalar(text("SELECT to_regclass('public.alembic_version')")) is not None:
            revisao = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        logger.info("schema conferido (revisao alembic=%s)", revisao or "desconhecida")


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
