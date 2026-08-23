"""Ambiente do Alembic.

Duas decisoes que valem registro:

- A URL vem de `Settings`, nunca do `alembic.ini`. O mesmo comando roda em
  local, staging e producao sem editar arquivo e sem senha versionada.
- O engine e assincrono (asyncpg), o mesmo driver da aplicacao. Um driver
  sincrono aqui significaria uma segunda DSN para manter em dia.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.db.base import Base
from app.models import domain  # noqa: F401  importa para registrar no metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `%` e escapado: senhas geradas costumam trazer o caractere, e o
# ConfigParser o interpretaria como interpolacao.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL.replace("%", "%%"))

target_metadata = Base.metadata


def _render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Faz o autogenerate escrever `pgvector.sqlalchemy.Vector` por extenso.

    Sem isto a revisao gerada sai com `Vector(dim)` sem import, e o
    `alembic upgrade` quebra com NameError na hora errada.
    """
    if type_ == "type" and obj.__class__.__module__.startswith("pgvector"):
        autogen_context.imports.add("import pgvector.sqlalchemy")  # type: ignore[attr-defined]
        return f"pgvector.sqlalchemy.Vector({obj.dim})"  # type: ignore[attr-defined]
    return False


def run_migrations_offline() -> None:
    """Gera SQL sem conectar (`alembic upgrade head --sql`)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=_render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_item=_render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
