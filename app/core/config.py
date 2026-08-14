"""Configuracao central da aplicacao (12-factor: tudo vem do ambiente)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Variaveis de ambiente tipadas e validadas na inicializacao."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Aplicacao ----
    PROJECT_NAME: str = "Document Intelligence Miner"
    API_V1_PREFIX: str = "/api/v1"
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # ---- Banco de dados ----
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "dim"
    POSTGRES_PASSWORD: str = "dim_secret"
    POSTGRES_DB: str = "dim_db"
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10

    # ---- Ingestao ----
    MAX_UPLOAD_SIZE_MB: int = 20
    CHUNK_SIZE: int = 1000
    CHUNK_OVERLAP: int = 150

    # ---- Embeddings / LLM (consumido na Fase 2) ----
    EMBEDDING_PROVIDER: Literal["openai", "huggingface"] = "openai"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    # Deve casar com a dimensao do modelo escolhido:
    #   text-embedding-3-small -> 1536 | all-MiniLM-L6-v2 -> 384
    EMBEDDING_DIM: int = 1536

    LLM_PROVIDER: Literal["openai"] = "openai"
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TEMPERATURE: float = 0.0
    OPENAI_API_KEY: str | None = None

    # ---- Recuperacao (RAG) ----
    RETRIEVAL_TOP_K: int = Field(default=4, ge=1, le=50)
    # Distancia de cosseno maxima aceita (0 = identico, 2 = oposto).
    RETRIEVAL_MAX_DISTANCE: float = Field(default=0.6, ge=0.0, le=2.0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def MAX_UPLOAD_SIZE_BYTES(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    @computed_field  # type: ignore[prop-decorator]
    @property
    def DATABASE_URL(self) -> str:
        """DSN assincrono (asyncpg) usado pelo SQLAlchemy."""
        return str(
            PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=self.POSTGRES_USER,
                password=self.POSTGRES_PASSWORD,
                host=self.POSTGRES_HOST,
                port=self.POSTGRES_PORT,
                path=self.POSTGRES_DB,
            )
        )


@lru_cache
def get_settings() -> Settings:
    """Instancia unica de Settings (cacheada) para uso como dependencia."""
    return Settings()


settings = get_settings()
