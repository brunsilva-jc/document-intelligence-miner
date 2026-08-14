"""Excecoes de dominio e seus handlers HTTP.

A camada de servico levanta excecoes de dominio (sem saber o que e HTTP);
a camada de API as traduz para respostas. Isso mantem o dominio testavel
fora do FastAPI.
"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger(__name__)


class DomainError(Exception):
    """Raiz de todos os erros de negocio da aplicacao."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    default_message: str = "Erro ao processar a requisicao."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class UnsupportedFileTypeError(DomainError):
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    default_message = "Formato de arquivo nao suportado."


class FileTooLargeError(DomainError):
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    default_message = "Arquivo excede o tamanho maximo permitido."


class EmptyDocumentError(DomainError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_message = "Nao foi possivel extrair texto do documento."


class DocumentNotFoundError(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    default_message = "Documento nao encontrado."


class NoRelevantContextError(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    default_message = "Nenhum trecho relevante encontrado para a pergunta."


class EmbeddingProviderError(DomainError):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_message = "Falha ao comunicar com o provedor de embeddings."


class LLMProviderError(DomainError):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_message = "Falha ao comunicar com o provedor de LLM."


def register_exception_handlers(app: FastAPI) -> None:
    """Registra os handlers globais na aplicacao."""

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, "error": type(exc).__name__},
        )

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("erro nao tratado em %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Erro interno do servidor.", "error": "InternalServerError"},
        )
