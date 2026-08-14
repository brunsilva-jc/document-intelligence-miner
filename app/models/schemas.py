"""Schemas Pydantic: contrato publico de entrada e saida da API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.domain import DocumentStatus


class HealthResponse(BaseModel):
    status: str = Field(examples=["ok"])
    service: str
    version: str


class ReadinessResponse(HealthResponse):
    database: str = Field(examples=["up", "down"])


class DocumentRead(BaseModel):
    """Representacao de um documento ja ingerido."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    status: DocumentStatus
    chunk_count: int
    error_message: str | None = None
    created_at: datetime


class DocumentList(BaseModel):
    total: int
    items: list[DocumentRead]


class UploadResponse(BaseModel):
    """Resultado da ingestao de um arquivo."""

    document: DocumentRead
    chunks_created: int = Field(ge=0)
    duplicated: bool = Field(
        default=False,
        description="True quando o checksum ja existia e o upload foi ignorado.",
    )


class AskRequest(BaseModel):
    """Pergunta do usuario ao acervo."""

    question: str = Field(min_length=3, max_length=2000)
    document_ids: list[uuid.UUID] | None = Field(
        default=None,
        description="Restringe a busca a documentos especificos.",
    )
    top_k: int | None = Field(default=None, ge=1, le=50)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "question": "Qual o prazo de vigencia previsto no contrato?",
                "top_k": 4,
            }
        }
    )


class SourceChunk(BaseModel):
    """Trecho usado como fundamento da resposta (citacao)."""

    model_config = ConfigDict(from_attributes=True)

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    content: str
    score: float = Field(description="Similaridade de cosseno (1.0 = identico).")
    page: int | None = Field(default=None, description="Pagina de origem, quando PDF.")


class AskResponse(BaseModel):
    question: str
    answer: str
    sources: list[SourceChunk]
    model: str
    elapsed_ms: int


class ErrorResponse(BaseModel):
    detail: str
    error: str
