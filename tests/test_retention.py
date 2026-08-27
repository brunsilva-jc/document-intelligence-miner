"""Testes da retencao do acervo.

Os limites de uso protegem a fatura; a retencao protege quem enviou o
arquivo. O que se verifica aqui e o que NAO fica no banco — e, com igual
cuidado, o que fica: uma politica que apaga demais e pior do que nenhuma.
"""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import settings
from app.models.domain import Document, DocumentChunk
from app.repositories.document_repository import DocumentRepository
from app.services.retention import (
    RetentionService,
    encerrar_rotina_de_retencao,
    iniciar_rotina_de_retencao,
)
from tests.factories import FakeDocumentRepository, make_document

AGORA = datetime.now(UTC)


@pytest.fixture
def repositorio() -> FakeDocumentRepository:
    return FakeDocumentRepository()


def povoar(repositorio: FakeDocumentRepository, *idades_em_dias: float) -> list[Document]:
    """Cria um documento por idade pedida, cada um com um chunk."""
    documentos = []
    for i, dias in enumerate(idades_em_dias):
        documento = make_document(
            filename=f"doc-{i}.pdf",
            checksum=f"sum-{i}",
            created_at=AGORA - timedelta(days=dias),
        )
        repositorio.documents[documento.id] = documento
        repositorio.chunks.append(
            DocumentChunk(
                id=uuid.uuid4(),
                document_id=documento.id,
                chunk_index=0,
                content=f"conteudo {i}",
                embedding=[0.0] * 8,
                chunk_metadata={},
            )
        )
        documentos.append(documento)
    return documentos


# --------------------------------------------------------------------------
# Politica: idade
# --------------------------------------------------------------------------
async def test_apaga_documento_vencido(repositorio: FakeDocumentRepository) -> None:
    novo, vencido = povoar(repositorio, 1, 10)

    resultado = await RetentionService(repositorio, max_age_days=7, max_documents=100).purge()

    assert resultado.por_idade == 1
    assert list(repositorio.documents) == [novo.id]
    assert vencido.id not in repositorio.documents


async def test_preserva_documento_dentro_do_prazo(repositorio: FakeDocumentRepository) -> None:
    """A vespera do vencimento ainda fica: o corte e em 7 dias, nao em 6."""
    povoar(repositorio, 6.9)

    resultado = await RetentionService(repositorio, max_age_days=7, max_documents=100).purge()

    assert resultado.total == 0
    assert len(repositorio.documents) == 1


async def test_apagar_documento_leva_os_chunks_junto(repositorio: FakeDocumentRepository) -> None:
    """No banco quem faz isso e o ON DELETE CASCADE — chunk orfao nao pode
    sobreviver ao documento, ou o /ask responderia citando o que ja foi
    apagado."""
    novo, _vencido = povoar(repositorio, 1, 30)

    await RetentionService(repositorio, max_age_days=7, max_documents=100).purge()

    assert [chunk.document_id for chunk in repositorio.chunks] == [novo.id]


# --------------------------------------------------------------------------
# Politica: teto de documentos
# --------------------------------------------------------------------------
async def test_teto_apaga_os_mais_antigos_primeiro(repositorio: FakeDocumentRepository) -> None:
    mais_novo, meio, mais_antigo = povoar(repositorio, 0.1, 0.2, 0.3)

    resultado = await RetentionService(repositorio, max_age_days=7, max_documents=2).purge()

    assert resultado.por_quota == 1
    assert mais_antigo.id not in repositorio.documents
    assert {mais_novo.id, meio.id} == set(repositorio.documents)


async def test_acervo_dentro_do_teto_fica_intacto(repositorio: FakeDocumentRepository) -> None:
    povoar(repositorio, 0.1, 0.2)

    resultado = await RetentionService(repositorio, max_age_days=7, max_documents=5).purge()

    assert resultado.total == 0
    assert len(repositorio.documents) == 2


async def test_idade_roda_antes_do_teto(repositorio: FakeDocumentRepository) -> None:
    """O teto conta o que sobreviveu a idade.

    Com tres documentos, um deles vencido e teto de 2: a idade apaga o
    vencido e sobram exatamente 2 — o teto nao tem mais nada a apagar. Na
    ordem inversa o mesmo acervo perderia um documento a mais.
    """
    povoar(repositorio, 0.1, 0.2, 30)

    resultado = await RetentionService(repositorio, max_age_days=7, max_documents=2).purge()

    assert (resultado.por_idade, resultado.por_quota) == (1, 0)
    assert len(repositorio.documents) == 2


async def test_acervo_vazio_nao_quebra(repositorio: FakeDocumentRepository) -> None:
    resultado = await RetentionService(repositorio, max_age_days=7, max_documents=1).purge()

    assert resultado.total == 0


# --------------------------------------------------------------------------
# SQL do repositorio
# --------------------------------------------------------------------------
class SessaoQueRegistra:
    """Sessao falsa que so guarda o SQL recebido, sem banco por perto."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, stmt):  # type: ignore[no-untyped-def]
        self.statements.append(
            str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        )
        return SimpleNamespace(rowcount=0)


async def test_delete_por_teto_preserva_os_mais_recentes() -> None:
    """Le o SQL de verdade: este e o unico DELETE do projeto sem WHERE por id.

    Um `ORDER BY` invertido no subselect apagaria exatamente o acervo que
    devia ficar, e os testes em memoria nao pegariam — o fake nao executa
    este SQL. Por isso a assercao e sobre o comando compilado.
    """
    sessao = SessaoQueRegistra()

    await DocumentRepository(sessao).delete_beyond_limit(3)  # type: ignore[arg-type]

    sql = " ".join(sessao.statements[0].split())
    assert "DELETE FROM documents WHERE (documents.id NOT IN (SELECT documents.id" in sql
    assert "ORDER BY documents.created_at DESC, documents.id DESC" in sql
    assert "LIMIT 3" in sql


async def test_teto_invalido_nao_gera_delete() -> None:
    """Teto zero significaria "NOT IN (lista vazia)" — um DELETE sem WHERE
    util, ou seja, o acervo inteiro."""
    sessao = SessaoQueRegistra()

    apagados = await DocumentRepository(sessao).delete_beyond_limit(0)  # type: ignore[arg-type]

    assert apagados == 0
    assert sessao.statements == []


async def test_delete_por_idade_filtra_por_created_at() -> None:
    sessao = SessaoQueRegistra()

    await DocumentRepository(sessao).delete_older_than(AGORA)  # type: ignore[arg-type]

    sql = " ".join(sessao.statements[0].split())
    assert sql.startswith("DELETE FROM documents WHERE documents.created_at <")


# --------------------------------------------------------------------------
# Tarefa de fundo
# --------------------------------------------------------------------------
@pytest.fixture
def retencao_desligada(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "RETENTION_ENABLED", False)
    yield


async def test_rotina_nao_sobe_quando_desligada(retencao_desligada: None) -> None:
    assert iniciar_rotina_de_retencao() is None


async def test_encerrar_rotina_desligada_e_no_op() -> None:
    await encerrar_rotina_de_retencao(None)


async def test_rotina_sobe_e_encerra_limpo(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tarefa precisa morrer no shutdown: uma varredura sobrevivente
    seguraria uma sessao com transacao aberta depois do `dispose_engine`."""
    varreduras = 0

    async def varredura_falsa() -> None:
        nonlocal varreduras
        varreduras += 1

    monkeypatch.setattr(settings, "RETENTION_ENABLED", True)
    monkeypatch.setattr(settings, "RETENTION_SWEEP_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr("app.services.retention.executar_varredura", varredura_falsa)

    tarefa = iniciar_rotina_de_retencao()
    assert tarefa is not None
    # Cede o controle para o laco rodar a varredura do boot e cair no sleep.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await encerrar_rotina_de_retencao(tarefa)

    assert varreduras == 1
    assert tarefa.cancelled() or tarefa.done()


async def test_falha_de_varredura_nao_derruba_o_laco(monkeypatch: pytest.MonkeyPatch) -> None:
    """Banco fora do ar adia a limpeza; nao a cancela pelo resto da vida
    do processo."""
    tentativas = 0

    async def varredura_que_falha() -> None:
        nonlocal tentativas
        tentativas += 1
        raise RuntimeError("banco fora do ar")

    monkeypatch.setattr(settings, "RETENTION_ENABLED", True)
    # Intervalo minimo para a segunda tentativa acontecer dentro do teste.
    monkeypatch.setattr(settings, "RETENTION_SWEEP_INTERVAL_SECONDS", 0)
    monkeypatch.setattr("app.services.retention.executar_varredura", varredura_que_falha)

    tarefa = iniciar_rotina_de_retencao()
    assert tarefa is not None
    for _ in range(10):
        await asyncio.sleep(0)

    await encerrar_rotina_de_retencao(tarefa)

    assert tentativas > 1
