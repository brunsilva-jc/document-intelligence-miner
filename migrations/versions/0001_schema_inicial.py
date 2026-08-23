"""schema inicial: documents, document_chunks e o indice HNSW

Revision ID: 0001
Revises:
Create Date: 2026-08-23

Escrita a mao (nao por autogenerate) porque a primeira revisao precisa criar
a extensao `vector` ANTES de qualquer coluna que use o tipo — ordem que o
autogenerate nao conhece.

Sobre a dimensao do embedding: ela vem de `settings.EMBEDDING_DIM`, igual ao
modelo ORM. Isso torna esta revisao dependente do ambiente, o que normalmente
seria um defeito — aqui e o mal menor, porque schema e modelo TEM de concordar
ou todo INSERT falha. Trocar de modelo de embedding depois exige uma revisao
nova e reindexar o acervo inteiro; nao ha migracao barata para isso.
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.config import settings

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A aplicacao tambem garante a extensao no startup; aqui ela precisa
    # existir antes das colunas Vector abaixo.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "processing",
                "completed",
                "failed",
                name="document_status",
                native_enum=False,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "doc_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("checksum"),
    )
    op.create_index("ix_documents_status", "documents", ["status"])

    op.create_table(
        "document_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(settings.EMBEDDING_DIM), nullable=False),
        sa.Column(
            "chunk_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_chunk_position"),
    )
    op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id"])

    # Indice ANN por cosseno. Criado com a tabela vazia, onde custa
    # milissegundos; no acervo cheio o mesmo comando trava a tabela por
    # minutos e consome muito maintenance_work_mem.
    op.create_index(
        "ix_document_chunks_embedding_hnsw",
        "document_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_document_chunks_embedding_hnsw", table_name="document_chunks")
    op.drop_index("ix_document_chunks_document_id", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("ix_documents_status", table_name="documents")
    op.drop_table("documents")
    # A extensao `vector` NAO e removida de proposito: derrubar uma extensao
    # do banco inteiro por causa de um rollback de aplicacao e escopo demais.
