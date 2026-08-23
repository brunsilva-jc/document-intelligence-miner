"""Testes da camada de configuracao."""

import pytest
from pydantic import ValidationError

from app.core.config import INSECURE_DEV_PASSWORD, MIN_API_KEY_LENGTH, Settings


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


# ---------------------------------------------------------------------------
# Guardas de producao
#
# `_env_file=None` em todos: sem isso o `.env` do desenvolvedor entra no
# teste e o resultado muda de maquina para maquina.
# ---------------------------------------------------------------------------

PROD_OK = {
    "ENVIRONMENT": "production",
    "API_KEY": "k" * MIN_API_KEY_LENGTH,
    "POSTGRES_PASSWORD": "senha-real-de-producao",
    "OPENAI_API_KEY": "sk-teste",
}


def test_producao_completa_e_aceita() -> None:
    settings = Settings(_env_file=None, **PROD_OK)

    assert settings.ENVIRONMENT == "production"


def test_local_nao_exige_nada() -> None:
    """O ambiente de desenvolvimento continua subindo sem ceremonia."""
    settings = Settings(_env_file=None, ENVIRONMENT="local")

    assert settings.API_KEY is None


def test_producao_sem_api_key_falha_no_boot() -> None:
    with pytest.raises(ValidationError, match="API_KEY nao definida"):
        Settings(_env_file=None, **{**PROD_OK, "API_KEY": None})


def test_producao_com_api_key_curta_falha_no_boot() -> None:
    with pytest.raises(ValidationError, match="minimo"):
        Settings(_env_file=None, **{**PROD_OK, "API_KEY": "curta"})


def test_producao_com_senha_de_desenvolvimento_falha_no_boot() -> None:
    """A senha do compose de dev esta no repositorio publico."""
    with pytest.raises(ValidationError, match="senha de desenvolvimento"):
        Settings(_env_file=None, **{**PROD_OK, "POSTGRES_PASSWORD": INSECURE_DEV_PASSWORD})


def test_producao_sem_openai_key_falha_no_boot() -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(_env_file=None, **{**PROD_OK, "OPENAI_API_KEY": None})


def test_erro_reune_todos_os_problemas_de_uma_vez() -> None:
    """Uma mensagem por deploy quebrado, nao uma por tentativa."""
    with pytest.raises(ValidationError) as exc:
        Settings(
            _env_file=None,
            ENVIRONMENT="production",
            POSTGRES_PASSWORD=INSECURE_DEV_PASSWORD,
        )

    mensagem = str(exc.value)
    assert "API_KEY" in mensagem
    assert "POSTGRES_PASSWORD" in mensagem
    assert "OPENAI_API_KEY" in mensagem
