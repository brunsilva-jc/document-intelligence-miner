"""Erro agregado e alerta de custo.

Os logs estruturados de `app/core/logging.py` respondem "o que aconteceu"
para quem ja esta olhando. Este modulo responde outra coisa: **quem avisa**.
Numa instancia que roda sozinha num VPS, a unica testemunha de um erro e o
`docker logs` de quem lembrar de abrir — ou seja, ninguem, ate um estranho
reclamar.

Sao duas coisas distintas, e so a primeira e um erro:

- **Erro agregado** (Sentry): excecao nao tratada vira evento com stack e
  contexto, agrupada por assinatura. O DSN e configuravel de proposito —
  serve tanto o Sentry SaaS quanto um GlitchTip auto-hospedado, que fala
  o mesmo protocolo. `SENTRY_DSN` vazio desliga o envio sem quebrar nada.
- **Alerta de custo**: o teto diario (`RATE_LIMIT_METERED_DAILY`) sendo
  consumido depressa nao e erro nenhum — nada falha, nada retorna 500. E
  exatamente por isso que precisa de alerta proprio: e o sinal de que a
  demo virou alvo, e ele passa despercebido justamente porque tudo esta
  funcionando como projetado.

**O que nao pode sair daqui.** Um relatorio de erro carrega os cabecalhos
da requisicao, e um deles e o `X-API-Key` da demo. Mandar isso para um
servico de terceiros e vazar a credencial para fora — de graca, e sem
ninguem perceber, porque o envio e silencioso e o painel e privado. Por
isso `send_default_pii=False` e o `_filtrar_evento` abaixo, que apaga os
cabecalhos sensiveis antes do evento sair da maquina.

Filtrar cabecalho, porem, **nao basta**, e isso foi medido e nao suposto:
com um coletor local no lugar do Sentry, a chave continuava saindo pelas
**variaveis locais do stack trace**. O SDK serializa os locais de cada
quadro, e os quadros de uma requisicao ASGI carregam o `scope` inteiro —
com os cabecalhos crus, em bytes, que o filtro de cabecalho nunca ve.

Dai `include_local_variables=False`. O custo e real: o painel deixa de
mostrar o valor das variaveis no momento do erro, que e justamente o que
torna um stack trace investigavel. Vale mesmo assim, porque nesta
aplicacao os locais nao guardam so a credencial — guardam o texto
extraido do PDF de um terceiro, os chunks e os embeddings dele. Mandar
isso para fora e exatamente o que o `send_default_pii=False` existia para
impedir.
"""

import logging
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.types import Event, Hint

from app.__version__ import __version__
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Cabecalhos que nunca acompanham um evento. Comparados em minusculas.
CABECALHOS_FILTRADOS = frozenset({"x-api-key", "authorization", "cookie", "proxy-authorization"})

VALOR_FILTRADO = "[filtrado]"

# Evita reinicializar o SDK quando `create_app()` e chamado mais de uma vez
# (os testes fazem isso a cada caso).
_configurado = False


def _filtrar_evento(evento: Event, _hint: Hint) -> Event | None:
    """Apaga credenciais do evento antes do envio.

    Roda em todo evento, inclusive nos que o proprio SDK monta sozinho.
    Falhar aqui nao pode derrubar a aplicacao: erro dentro do relator de
    erros e o tipo de coisa que se descobre tarde, entao a funcao e
    deliberadamente burra — so troca strings de um dicionario.
    """
    requisicao: Any = evento.get("request") or {}
    cabecalhos = requisicao.get("headers")
    if isinstance(cabecalhos, dict):
        for nome in list(cabecalhos):
            if nome.lower() in CABECALHOS_FILTRADOS:
                cabecalhos[nome] = VALOR_FILTRADO
    return evento


def configurar_observabilidade() -> bool:
    """Liga o Sentry se houver DSN. Devolve se ficou ligado.

    Chamada dentro de `create_app()`, ANTES de a aplicacao existir: as
    integracoes do SDK instrumentam o Starlette no momento do `init`, e
    uma aplicacao ja construida nao seria alcancada por elas.
    """
    global _configurado

    if not settings.SENTRY_DSN:
        logger.info("sentry desligado (SENTRY_DSN vazio): erros ficam so no log local")
        return False

    if _configurado:
        return True

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        # Versao da aplicacao: e o que agrupa "isto quebrou no deploy de
        # ontem" em vez de "isto quebra as vezes".
        release=f"document-intelligence-miner@{__version__}",
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        # PII desligado: sem corpo de requisicao, sem cookie, sem IP. O
        # acervo e de terceiros e o corpo de um /upload e o documento
        # inteiro de alguem.
        send_default_pii=False,
        # Ver a nota no topo do modulo: sem isto a chave de API sai pelo
        # `scope` serializado nos locais do stack trace, e junto com ela o
        # documento de quem fez o upload.
        include_local_variables=False,
        before_send=_filtrar_evento,
        integrations=[
            StarletteIntegration(),
            FastApiIntegration(),
            # A varredura de retencao vive numa tarefa de fundo; sem esta
            # integracao, uma falha la dentro nao teria contexto nenhum.
            AsyncioIntegration(),
            # WARNING vira migalha de contexto, ERROR vira evento. E o que
            # faz `logger.exception` da retencao chegar sozinho.
            LoggingIntegration(level=None, event_level=logging.ERROR),
        ],
    )

    _configurado = True
    logger.info(
        "sentry ligado (env=%s, traces=%s)",
        settings.ENVIRONMENT,
        settings.SENTRY_TRACES_SAMPLE_RATE,
    )
    return True


def relatar_excecao(exc: BaseException) -> None:
    """Manda uma excecao ja tratada para o agregador.

    Necessario porque `register_exception_handlers` captura TUDO: com um
    handler para `Exception`, nada sobe ate o middleware do SDK, e o erro
    que a aplicacao transforma em 500 educado sairia invisivel.
    """
    sentry_sdk.capture_exception(exc)


def alertar_custo(*, escopo: str, acessos: int, limite: int, esgotado: bool) -> None:
    """Avisa que o teto que protege a fatura esta sendo consumido.

    Emitido no maximo duas vezes por janela (ao cruzar o limiar e ao
    esgotar) — quem chama cuida disso. Um alerta por requisicao recusada
    seria ruido, e ruido e como um alerta deixa de ser lido.
    """
    fracao = acessos / limite if limite else 1.0
    mensagem = (
        f"teto de {escopo} esgotado ({acessos}/{limite})"
        if esgotado
        else f"teto de {escopo} em {fracao:.0%} ({acessos}/{limite})"
    )

    # ERROR quando esgotou: e o nivel que a LoggingIntegration transforma
    # em evento sozinha. Antes disso e WARNING — merece atencao, nao
    # plantao.
    if esgotado:
        logger.error("ALERTA DE CUSTO: %s", mensagem)
    else:
        logger.warning("ALERTA DE CUSTO: %s", mensagem)

    sentry_sdk.capture_message(
        f"alerta de custo: {mensagem}",
        level="error" if esgotado else "warning",
    )
