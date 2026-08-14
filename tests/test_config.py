"""Testes da camada de configuracao."""

from app.core.config import Settings


def test_database_url_uses_asyncpg_driver() -> None:
    settings = Settings(
        POSTGRES_HOST="db",
        POSTGRES_PORT=5432,
        POSTGRES_USER="dim",
        POSTGRES_PASSWORD="secret",
        POSTGRES_DB="dim_db",
    )

    assert settings.DATABASE_URL == "postgresql+asyncpg://dim:secret@db:5432/dim_db"


def test_max_upload_size_is_converted_to_bytes() -> None:
    settings = Settings(MAX_UPLOAD_SIZE_MB=5)

    assert settings.MAX_UPLOAD_SIZE_BYTES == 5 * 1024 * 1024
