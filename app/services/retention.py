"""Retencao do acervo: o que a demo publica NAO guarda para sempre.

Aqui nao se protege fatura — isso e trabalho dos limites de uso. O que se
protege e quem enviou o arquivo. Uma instancia de demonstracao aberta
recebe documento de estranho, e documento de estranho e dado de terceiro:
guarda-lo indefinidamente porque ninguem escreveu a linha que o apaga e
uma decisao, so que tomada por omissao. O disco e a segunda razao, e a
menos importante.

Sao duas regras, aplicadas nesta ordem:

- **Idade** (`RETENTION_MAX_AGE_DAYS`) — todo documento vence. E a regra
  que de fato limita por quanto tempo o dado de alguem fica no banco.
- **Teto de documentos** (`RETENTION_MAX_DOCUMENTS`) — os mais antigos
  saem primeiro. Serve para o que a idade nao pega: uma enxurrada dentro
  da mesma janela, toda ela nova demais para vencer.

A ordem importa. A idade roda primeiro, e o teto conta o que sobreviveu a
ela — invertido, o teto apagaria por quota documentos que a idade ja ia
apagar de graca, e o numero relatado por varredura ficaria sem sentido.

A varredura roda em uma tarefa de fundo do proprio processo, iniciada no
`lifespan`, e nao em cron. E uma escolha com um custo dito em voz alta:
mais de um worker do uvicorn significaria uma varredura por worker,
apagando o mesmo acervo em paralelo (o DELETE e idempotente, entao o
resultado e correto, mas o trabalho e repetido). O `Dockerfile` sobe um
worker so, como nos limites de uso. Passar disso pede mover isto para um
cron externo ou para um lock no Postgres.
"""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import AsyncSessionLocal
from app.repositories.document_repository import DocumentRepository

logger = get_logger(__name__)

# Nome da tarefa de fundo — aparece em traceback e em `asyncio.all_tasks()`.
NOME_DA_TAREFA = "retencao-do-acervo"


@dataclass(slots=True, frozen=True)
class ResultadoDaVarredura:
    """Quantos documentos cada regra apagou nesta passada."""

    por_idade: int = 0
    por_quota: int = 0

    @property
    def total(self) -> int:
        return self.por_idade + self.por_quota


class RetentionService:
    """Aplica a politica de retencao sobre o acervo.

    Recebe o repositorio pronto, como os demais services: a politica e
    testavel com um repositorio em memoria, sem PostgreSQL por perto.
    """

    def __init__(
        self,
        repository: DocumentRepository,
        *,
        max_age_days: int | None = None,
        max_documents: int | None = None,
    ) -> None:
        self._repository = repository
        self._max_age_days = max_age_days or settings.RETENTION_MAX_AGE_DAYS
        self._max_documents = max_documents or settings.RETENTION_MAX_DOCUMENTS

    async def purge(self) -> ResultadoDaVarredura:
        """Aplica idade e depois teto. Nao comita — quem chama decide."""
        corte = datetime.now(UTC) - timedelta(days=self._max_age_days)

        por_idade = await self._repository.delete_older_than(corte)
        por_quota = await self._repository.delete_beyond_limit(self._max_documents)

        resultado = ResultadoDaVarredura(por_idade=por_idade, por_quota=por_quota)
        if resultado.total:
            logger.info(
                "retencao: %d documento(s) removido(s) (%d por idade > %dd, "
                "%d por teto de %d no acervo)",
                resultado.total,
                resultado.por_idade,
                self._max_age_days,
                resultado.por_quota,
                self._max_documents,
            )
        return resultado


async def executar_varredura() -> ResultadoDaVarredura:
    """Roda uma varredura em sessao propria e comita.

    Sessao propria porque isto nao acontece dentro de uma requisicao: nao
    ha `get_session` para fornecer a unit of work nem commit no fim do
    request para aproveitar.
    """
    async with AsyncSessionLocal() as session:
        resultado = await RetentionService(DocumentRepository(session)).purge()
        await session.commit()
        return resultado


async def _varrer_periodicamente() -> None:
    """Laco da tarefa de fundo: varre, dorme, repete.

    Varre ANTES de dormir de proposito. Um processo que passou dias fora
    do ar volta com documentos ja vencidos no banco; esperar o primeiro
    intervalo para limpa-los seria guardar dado alheio por mais tempo
    justamente no caso em que ele ja passou do prazo.

    Falha de varredura nao derruba o laco: banco fora do ar por um
    momento adia a limpeza, nao a cancela para o resto da vida do
    processo. `CancelledError` e a excecao — essa e o desligamento
    pedindo passagem, e reerguer e como a tarefa termina.
    """
    while True:
        try:
            await executar_varredura()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("varredura de retencao falhou; nova tentativa no proximo intervalo")

        await asyncio.sleep(settings.RETENTION_SWEEP_INTERVAL_SECONDS)


def iniciar_rotina_de_retencao() -> asyncio.Task | None:
    """Poe a varredura periodica no ar. `None` quando desligada."""
    if not settings.RETENTION_ENABLED:
        logger.warning(
            "retencao desligada: documentos enviados ficam no banco ate serem apagados a mao"
        )
        return None

    logger.info(
        "retencao ligada: %d dia(s) de idade, teto de %d documentos, varredura a cada %ds",
        settings.RETENTION_MAX_AGE_DAYS,
        settings.RETENTION_MAX_DOCUMENTS,
        settings.RETENTION_SWEEP_INTERVAL_SECONDS,
    )
    return asyncio.create_task(_varrer_periodicamente(), name=NOME_DA_TAREFA)


async def encerrar_rotina_de_retencao(tarefa: asyncio.Task | None) -> None:
    """Cancela a tarefa e ESPERA ela terminar.

    Sem o `await`, o desligamento seguiria com uma varredura no meio de um
    DELETE e a sessao seria coletada com a transacao aberta.
    """
    if tarefa is None:
        return

    tarefa.cancel()
    with suppress(asyncio.CancelledError):
        await tarefa
    logger.info("rotina de retencao encerrada")
