"""Limites de uso: protegem a conta do provedor, nao o servidor.

A chave de API responde "quem pode chamar"; ela nao responde "quanto".
Com a chave certa em maos, um laco de `/ask` gasta a `OPENAI_API_KEY` do
dono ate o teto do cartao — e uma instancia de demonstracao publica existe
justamente para ser chamada por estranhos.

Sao dois limites com finalidades diferentes:

- **Janela curta, por cliente** — evita que uma rajada monopolize o
  processo (a ingestao e sincrona: um upload segura a conexao).
- **Teto diario, global** — e o limite de GASTO. Global de proposito:
  o dinheiro e um so, entao somar por cliente nao protegeria nada.

O teto diario tambem AVISA, e nao so recusa: quem so descobre que a demo
virou alvo quando a conta chega descobriu tarde demais. O aviso sai duas
vezes por janela — ao cruzar `COST_ALERT_THRESHOLD` e ao esgotar — e quem
o entrega e `app/core/observability.py`.

Os contadores vivem em memoria, o que implica duas coisas ditas em voz
alta: reiniciar o processo zera o teto diario, e mais de um worker do
uvicorn multiplicaria o teto pelo numero de workers. O `Dockerfile` sobe
um worker so; passar disso exige mover estes contadores para o Postgres
ou um Redis.
"""

import hashlib
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache

from fastapi import Request

from app.core.config import settings
from app.core.exceptions import RateLimitExceededError
from app.core.logging import get_logger
from app.core.observability import alertar_custo
from app.core.security import API_KEY_HEADER_NAME

# Assinatura de quem recebe o alerta de consumo do teto.
Relator = Callable[..., None]

logger = get_logger(__name__)

# Acima disto o dicionario de janelas passa por uma poda das expiradas.
# Sem teto, um atacante trocando de IP faria o contador de abuso virar o
# proprio vazamento de memoria.
MAX_CLIENTES_RASTREADOS = 10_000

# Identidade unica do teto diario: o gasto e somado para todo mundo.
CLIENTE_GLOBAL = "global"


@dataclass(slots=True)
class _Janela:
    """Contador de uma janela fixa: quantos acessos e quando zera."""

    reset_em: float
    acessos: int
    # Cada alerta sai uma vez por janela. Sem estas duas travas, um cliente
    # insistente geraria um alerta por requisicao recusada — e alerta
    # repetido e como alerta deixa de ser lido.
    alerta_de_limiar_emitido: bool = field(default=False)
    alerta_de_esgotamento_emitido: bool = field(default=False)


class LimitadorJanelaFixa:
    """Conta acessos por chave dentro de uma janela de tempo fixa.

    Janela fixa e nao *sliding window*: um pouco mais permissiva na
    virada da janela, e muito mais simples de ler e de testar. Para
    conter custo e rajada, a diferenca nao importa.
    """

    def __init__(
        self,
        *,
        limite: int,
        janela_segundos: int,
        escopo: str,
        limiar_de_alerta: float | None = None,
        relator: Relator | None = None,
    ) -> None:
        self._limite = limite
        self._janela_segundos = janela_segundos
        self._escopo = escopo
        self._janelas: dict[str, _Janela] = {}
        # So o teto de gasto alerta. O limitador de rajada dispara o dia
        # inteiro por motivo banal — um cliente afobado nao e um incidente,
        # e alertar sobre ele afogaria o alerta que importa.
        self._relator = relator
        self._acessos_para_alertar = (
            math.ceil(limite * limiar_de_alerta) if limiar_de_alerta and relator else None
        )

    def registrar(self, cliente: str) -> None:
        """Contabiliza um acesso. Levanta 429 se o teto ja foi atingido."""
        # `monotonic` e nao `time()`: ajuste de relogio (NTP, horario de
        # verao) nao pode nem liberar nem prender ninguem.
        agora = time.monotonic()
        janela = self._janelas.get(cliente)

        if janela is None or agora >= janela.reset_em:
            self._podar(agora)
            janela = _Janela(reset_em=agora + self._janela_segundos, acessos=0)
            self._janelas[cliente] = janela

        if janela.acessos >= self._limite:
            faltam = max(1, math.ceil(janela.reset_em - agora))
            # O identificador do cliente ja chega derivado (hash da chave
            # ou IP): nao ha credencial em texto claro neste log.
            logger.warning(
                "limite %s excedido por %s (%d/%d), libera em %ds",
                self._escopo,
                cliente,
                janela.acessos,
                self._limite,
                faltam,
            )
            if self._relator and not janela.alerta_de_esgotamento_emitido:
                janela.alerta_de_esgotamento_emitido = True
                self._alertar(janela, esgotado=True)
            raise RateLimitExceededError(
                f"Limite de {self._limite} {self._escopo} excedido. "
                f"Tente novamente em {faltam}s.",
                retry_after=faltam,
            )

        janela.acessos += 1

        if (
            self._acessos_para_alertar is not None
            and not janela.alerta_de_limiar_emitido
            and janela.acessos >= self._acessos_para_alertar
        ):
            janela.alerta_de_limiar_emitido = True
            self._alertar(janela, esgotado=False)

    def _alertar(self, janela: _Janela, *, esgotado: bool) -> None:
        """Entrega o alerta sem deixar o relator derrubar a requisicao.

        O relator fala com um servico de fora. Se ele falhar, quem estava
        so tentando fazer um upload legitimo nao pode receber 500 por
        causa disso — a chamada que gastou a cota ja foi contada, e o
        limite continua valendo com ou sem aviso.
        """
        if self._relator is None:
            return
        try:
            self._relator(
                escopo=self._escopo,
                acessos=janela.acessos,
                limite=self._limite,
                esgotado=esgotado,
            )
        except Exception:  # pragma: no cover - depende do relator configurado
            logger.exception("falha ao emitir alerta de custo de %s", self._escopo)

    def zerar(self) -> None:
        """Descarta todos os contadores (usado pelos testes)."""
        self._janelas.clear()

    def _podar(self, agora: float) -> None:
        if len(self._janelas) < MAX_CLIENTES_RASTREADOS:
            return
        expiradas = [cliente for cliente, j in self._janelas.items() if agora >= j.reset_em]
        for cliente in expiradas:
            del self._janelas[cliente]
        logger.info("podadas %d janelas expiradas de %s", len(expiradas), self._escopo)


@dataclass(slots=True, frozen=True)
class Limitadores:
    requisicoes: LimitadorJanelaFixa
    operacoes_pagas: LimitadorJanelaFixa


@lru_cache
def get_limitadores() -> Limitadores:
    """Instancia unica por processo, construida a partir das settings.

    Cacheada como `get_settings`: os contadores precisam sobreviver entre
    requisicoes. `get_limitadores.cache_clear()` devolve o estado limpo,
    que e como os testes isolam um caso do outro.
    """
    return Limitadores(
        requisicoes=LimitadorJanelaFixa(
            limite=settings.RATE_LIMIT_REQUESTS,
            janela_segundos=settings.RATE_LIMIT_WINDOW_SECONDS,
            escopo="requisicoes",
        ),
        operacoes_pagas=LimitadorJanelaFixa(
            limite=settings.RATE_LIMIT_METERED_DAILY,
            janela_segundos=settings.RATE_LIMIT_METERED_WINDOW_SECONDS,
            escopo="operacoes pagas por dia",
            limiar_de_alerta=settings.COST_ALERT_THRESHOLD,
            relator=alertar_custo,
        ),
    )


def identificar_cliente(request: Request) -> str:
    """Quem esta chamando, para efeito de contagem.

    A chave de API tem precedencia sobre o IP: e a identidade que a
    aplicacao de fato autenticou. Entra como digest — o identificador vai
    para log, e log com credencial em texto claro e credencial vazada.

    Sem chave (so acontece em `local`), cai no IP do socket. Atras de um
    proxy reverso sem `--proxy-headers`, todo mundo colapsa no IP do
    proxy: o limite fica MAIS restrito, nunca mais permissivo. Confiar em
    `X-Forwarded-For` cru seria o contrario — o cabecalho e do cliente, e
    trocar de identidade a cada requisicao desligaria o limite.
    """
    chave = request.headers.get(API_KEY_HEADER_NAME)
    if chave:
        return "chave:" + hashlib.sha256(chave.encode("utf-8", "replace")).hexdigest()[:16]

    cliente = request.client.host if request.client else "desconhecido"
    return "ip:" + cliente


async def limitar_requisicoes(request: Request) -> None:
    """Teto de requisicoes por janela curta, por cliente."""
    if not settings.RATE_LIMIT_ENABLED:
        return
    get_limitadores().requisicoes.registrar(identificar_cliente(request))


async def limitar_operacoes_pagas(_: Request) -> None:
    """Teto diario das rotas que gastam tokens (`/upload` e `/ask`).

    Contado depois do limite de janela curta e so nas rotas caras: listar
    ou apagar documento nao custa nada no provedor e nao deve consumir a
    cota que protege a fatura.
    """
    if not settings.RATE_LIMIT_ENABLED:
        return
    get_limitadores().operacoes_pagas.registrar(CLIENTE_GLOBAL)


def reset_rate_limits() -> None:
    """Reconstroi os limitadores a partir das settings atuais."""
    get_limitadores.cache_clear()
