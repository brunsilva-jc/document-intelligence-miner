"""Camada de apresentacao: rotas HTTP.

As rotas apenas validam entrada, chamam um service e serializam a saida.
Nenhuma regra de negocio ou SQL vive aqui.
"""

import uuid

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status

from app.__version__ import __version__
from app.api.deps import DocumentServiceDep, RagEngineDep
from app.core.config import settings
from app.core.security import require_api_key
from app.db.session import check_db_connection
from app.models.schemas import (
    AskRequest,
    AskResponse,
    DocumentList,
    DocumentRead,
    ErrorResponse,
    HealthResponse,
    ReadinessResponse,
    SourceChunk,
    UploadResponse,
)

health_router = APIRouter(tags=["health"])

# A chave e exigida no router, nao rota a rota: uma rota nova nasce
# protegida por padrao, e esquecer de proteger deixa de ser possivel.
documents_router = APIRouter(
    prefix="/documents",
    tags=["documents"],
    dependencies=[Depends(require_api_key)],
    responses={401: {"model": ErrorResponse}},
)


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------
@health_router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    """Responde enquanto o processo estiver vivo (nao toca no banco)."""
    return HealthResponse(status="ok", service=settings.PROJECT_NAME, version=__version__)


# HEAD precisa de rota propria: `.get()` registra so GET, e monitor externo
# (UptimeRobot, por exemplo) sonda com HEAD por padrao — levaria 405 e
# acusaria queda com o servico de pe. Fora do schema para nao duplicar a
# operacao no OpenAPI; a resposta a HEAD nao tem corpo mesmo.
@health_router.head("/health", include_in_schema=False)
async def health_head() -> Response:
    """Mesmo contrato do GET, sem corpo."""
    return Response(status_code=status.HTTP_200_OK)


@health_router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={503: {"model": ReadinessResponse}},
)
async def readiness() -> ReadinessResponse:
    """Verifica dependencias externas (PostgreSQL)."""
    db_up = await check_db_connection()
    return ReadinessResponse(
        status="ok" if db_up else "degraded",
        service=settings.PROJECT_NAME,
        version=__version__,
        database="up" if db_up else "down",
    )


# --------------------------------------------------------------------------
# Documentos
# --------------------------------------------------------------------------
@documents_router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingere um PDF ou TXT: extrai, divide em chunks e vetoriza",
    responses={
        413: {"model": ErrorResponse},
        415: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
async def upload_document(
    service: DocumentServiceDep,
    file: UploadFile = File(..., description="Arquivo PDF ou texto (.txt/.md)"),
) -> UploadResponse:
    """Extrai o texto, divide em blocos, vetoriza e persiste.

    A ingestao e sincrona: o cliente so recebe 201 quando os embeddings
    ja estao no banco. Para acervos grandes isso vira uma fila (Fase 3).
    """
    data = await file.read()
    result = await service.ingest(
        filename=file.filename or "sem-nome",
        content_type=file.content_type,
        data=data,
    )
    return UploadResponse(
        document=DocumentRead.model_validate(result.document),
        chunks_created=result.chunks_created,
        duplicated=result.duplicated,
    )


@documents_router.post(
    "/ask",
    response_model=AskResponse,
    summary="Responde uma pergunta usando RAG sobre os documentos ingeridos",
    responses={404: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
)
async def ask_documents(payload: AskRequest, engine: RagEngineDep) -> AskResponse:
    """Responde com base apenas nos trechos recuperados do acervo."""
    result = await engine.answer(
        payload.question,
        top_k=payload.top_k,
        document_ids=payload.document_ids,
    )
    return AskResponse(
        question=result.question,
        answer=result.answer,
        model=result.model,
        elapsed_ms=result.elapsed_ms,
        sources=[
            SourceChunk(
                chunk_id=match.chunk.id,
                document_id=match.chunk.document_id,
                filename=match.filename,
                chunk_index=match.chunk.chunk_index,
                content=match.chunk.content,
                score=round(match.score, 4),
                page=match.chunk.chunk_metadata.get("page"),
            )
            for match in result.matches
        ],
    )


@documents_router.get("", response_model=DocumentList, summary="Lista documentos")
async def list_documents(
    service: DocumentServiceDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> DocumentList:
    total, items = await service.list_documents(limit=limit, offset=offset)
    return DocumentList(total=total, items=[DocumentRead.model_validate(item) for item in items])


@documents_router.get(
    "/{document_id}",
    response_model=DocumentRead,
    summary="Detalha um documento",
    responses={404: {"model": ErrorResponse}},
)
async def get_document(document_id: uuid.UUID, service: DocumentServiceDep) -> DocumentRead:
    document = await service.get_document(document_id)
    return DocumentRead.model_validate(document)


@documents_router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove o documento e seus chunks",
    responses={404: {"model": ErrorResponse}},
)
async def delete_document(document_id: uuid.UUID, service: DocumentServiceDep) -> None:
    await service.delete_document(document_id)


api_router = APIRouter()
api_router.include_router(documents_router)
