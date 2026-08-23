"""Autenticacao por chave de API.

As rotas de documentos sao *metered*: cada `/upload` e cada `/ask` gastam
tokens pagos no provedor. Sem chave, uma instancia publica e uma conta de
API aberta na internet — junto com o `/docs` que ensina a usa-la.

`/health` fica de fora de proposito: o monitor externo precisa dele sem
credencial.
"""

import secrets

from fastapi import Security, status
from fastapi.security import APIKeyHeader

from app.core.config import settings
from app.core.exceptions import DomainError
from app.core.logging import get_logger

logger = get_logger(__name__)

API_KEY_HEADER_NAME = "X-API-Key"

# `auto_error=False` para que a recusa passe pelo nosso handler de dominio
# e saia no mesmo formato {"detail", "error"} das outras respostas de erro.
_api_key_header = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)


class InvalidApiKeyError(DomainError):
    """Chave ausente, malformada ou que nao confere."""

    status_code = status.HTTP_401_UNAUTHORIZED
    default_message = "Chave de API ausente ou invalida."


async def require_api_key(provided: str | None = Security(_api_key_header)) -> None:
    """Exige o header `X-API-Key` quando ha uma chave configurada.

    Sem `API_KEY` a checagem fica desligada — conveniencia de
    desenvolvimento. Fora de `local` esse caminho nao existe: `Settings`
    recusa subir sem chave (ver `app/core/config.py`).
    """
    expected = settings.API_KEY
    if not expected:
        return

    # Comparacao em bytes e de tempo constante: `compare_digest` com `str`
    # rejeita caractere nao-ASCII (viraria 500 num header malformado), e a
    # comparacao ingenua com `!=` vaza o tamanho do prefixo correto.
    if not provided or not secrets.compare_digest(provided.encode(), expected.encode()):
        # A chave recebida NAO entra no log: viraria credencial em disco.
        logger.warning("requisicao recusada: chave de API ausente ou invalida")
        raise InvalidApiKeyError()
