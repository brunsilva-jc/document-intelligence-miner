"""Configuracao central da aplicacao (12-factor: tudo vem do ambiente)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Senha do compose de desenvolvimento. Serve para subir rapido na maquina do
# dev e para mais nada — fora de `local` e recusada no boot.
INSECURE_DEV_PASSWORD = "dim_secret"

# Abaixo disso a chave e forca-brutavel. `secrets.token_urlsafe(32)` da 43.
MIN_API_KEY_LENGTH = 32


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

    # ---- Seguranca ----
    # Exigida no header X-API-Key em toda rota de documentos. Vazia desliga
    # a checagem, o que so e aceito em ENVIRONMENT=local.
    API_KEY: str | None = None
    # Origens liberadas no CORS. Vazio = nenhuma, que e o certo para uma API
    # consumida por servidor. So preencha se um navegador for chamar isto.
    CORS_ALLOW_ORIGINS: list[str] = []

    # ---- Limites de uso ----
    # A chave diz QUEM pode chamar; estes tetos dizem QUANTO. Sem eles,
    # uma chave vazada (ou uma demo publica) e um laco de /ask gastando a
    # conta do provedor ate o limite do cartao.
    RATE_LIMIT_ENABLED: bool = True
    # Janela curta, por cliente: contem rajada.
    RATE_LIMIT_REQUESTS: int = Field(default=60, ge=1)
    RATE_LIMIT_WINDOW_SECONDS: int = Field(default=60, ge=1)
    # Teto das rotas que gastam tokens (/upload e /ask). Global, porque o
    # que se protege e uma fatura so.
    RATE_LIMIT_METERED_DAILY: int = Field(default=200, ge=1)
    RATE_LIMIT_METERED_WINDOW_SECONDS: int = Field(default=86_400, ge=1)

    # ---- Observabilidade ----
    # Sem isto, a unica testemunha de um erro em producao e o `docker logs`
    # de quem lembrar de olhar. DSN vazio desliga o envio e nao quebra nada:
    # a aplicacao continua logando igual.
    SENTRY_DSN: str | None = None
    # Fracao de requisicoes com tracing. 0.0 = so erros, que e o que uma
    # demo precisa; performance aqui nao paga o volume de eventos.
    SENTRY_TRACES_SAMPLE_RATE: float = Field(default=0.0, ge=0.0, le=1.0)
    # Fracao do teto diario a partir da qual o consumo vira alerta. O teto
    # em si ja e tarde: quando ele bate, a demo passou o dia inteiro sendo
    # usada por alguem e so agora alguem fica sabendo.
    COST_ALERT_THRESHOLD: float = Field(default=0.8, gt=0.0, le=1.0)

    # ---- Retencao do acervo ----
    # A demo e publica: quem sobe documento deixa dado de terceiro no banco.
    # Nada aqui protege a fatura — protege quem enviou o arquivo, e o disco.
    RETENTION_ENABLED: bool = True
    # Idade maxima de um documento. Vencido, ele e apagado com seus chunks.
    RETENTION_MAX_AGE_DAYS: int = Field(default=7, ge=1)
    # Teto de documentos no acervo: passando disso, os mais antigos saem
    # primeiro. E o limite que segura uma enxurrada dentro da mesma janela.
    RETENTION_MAX_DOCUMENTS: int = Field(default=100, ge=1)
    # De quanto em quanto tempo a varredura roda. A primeira e no boot.
    RETENTION_SWEEP_INTERVAL_SECONDS: int = Field(default=3_600, ge=60)

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

    @model_validator(mode="after")
    def _exigir_configuracao_de_producao(self) -> "Settings":
        """Falha no BOOT, nao na primeira requisicao.

        Os tres erros abaixo tem em comum serem invisiveis: a aplicacao
        sobe, responde /health e parece saudavel. Sem `API_KEY` ela e uma
        API paga aberta; com a senha de desenvolvimento ela e um Postgres
        de senha publicada; sem `OPENAI_API_KEY` todo /ask responde 502.
        Barato de achar agora, caro de achar com o servico no ar.
        """
        if self.ENVIRONMENT == "local":
            return self

        problemas: list[str] = []

        if not self.API_KEY:
            problemas.append(
                "API_KEY nao definida (gere uma com: python -c "
                "'import secrets; print(secrets.token_urlsafe(32))')"
            )
        elif len(self.API_KEY) < MIN_API_KEY_LENGTH:
            problemas.append(
                f"API_KEY tem {len(self.API_KEY)} caracteres, " f"o minimo e {MIN_API_KEY_LENGTH}"
            )

        if self.POSTGRES_PASSWORD == INSECURE_DEV_PASSWORD:
            problemas.append("POSTGRES_PASSWORD ainda e a senha de desenvolvimento")

        if not self.OPENAI_API_KEY and "openai" in (self.EMBEDDING_PROVIDER, self.LLM_PROVIDER):
            problemas.append("OPENAI_API_KEY nao definida, mas um provedor OpenAI esta ativo")

        if problemas:
            raise ValueError(
                f"configuracao invalida para ENVIRONMENT={self.ENVIRONMENT}: "
                + "; ".join(problemas)
            )

        return self

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
