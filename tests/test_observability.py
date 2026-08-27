"""Testes da observabilidade: erro agregado e alerta de custo.

Duas garantias distintas. A primeira e que o evento SAI — um erro que a
aplicacao transforma em 500 educado nao pode sair invisivel para quem
mantem o servico. A segunda, e mais importante, e o que NAO sai junto
com ele: o relatorio carrega os cabecalhos da requisicao, e um deles e a
credencial da demo. Mandar isso para um servico de terceiros e vazar a
chave de graca, em silencio.
"""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import observability
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.observability import (
    VALOR_FILTRADO,
    _filtrar_evento,
    alertar_custo,
    configurar_observabilidade,
)
from app.core.rate_limit import LimitadorJanelaFixa
from app.core.security import API_KEY_HEADER_NAME


# --------------------------------------------------------------------------
# Filtro de credenciais
# --------------------------------------------------------------------------
def test_filtro_apaga_a_chave_de_api() -> None:
    evento = _filtrar_evento(
        {"request": {"headers": {"X-API-Key": "chave-secreta-de-verdade"}}}, {}
    )

    assert evento["request"]["headers"]["X-API-Key"] == VALOR_FILTRADO


def test_filtro_nao_depende_da_caixa_do_cabecalho() -> None:
    """HTTP nao distingue maiuscula de minuscula em nome de cabecalho, e
    um cliente qualquer manda `x-api-key`."""
    evento = _filtrar_evento(
        {"request": {"headers": {"x-api-key": "a", "AUTHORIZATION": "b", "Cookie": "c"}}}, {}
    )

    assert set(evento["request"]["headers"].values()) == {VALOR_FILTRADO}


def test_filtro_preserva_o_resto_do_evento() -> None:
    """Filtrar demais tambem tem custo: sem User-Agent nem caminho, o
    evento chega sem o contexto que o torna investigavel."""
    evento = _filtrar_evento(
        {
            "request": {
                "url": "http://demo/api/v1/documents",
                "headers": {"X-API-Key": "segredo", "User-Agent": "curl/8"},
            },
            "level": "error",
        },
        {},
    )

    assert evento["request"]["headers"]["User-Agent"] == "curl/8"
    assert evento["request"]["url"] == "http://demo/api/v1/documents"
    assert evento["level"] == "error"


def test_filtro_aguenta_evento_sem_requisicao() -> None:
    """Evento de tarefa de fundo (a varredura de retencao, por exemplo)
    nao tem `request` nenhum."""
    assert _filtrar_evento({"level": "error"}, {}) == {"level": "error"}


# --------------------------------------------------------------------------
# Configuracao
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def sdk_isolado(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Nenhum teste deixa o SDK inicializado para o proximo."""
    monkeypatch.setattr(observability, "_configurado", False)
    yield


def test_sem_dsn_nao_inicializa(monkeypatch: pytest.MonkeyPatch) -> None:
    """O padrao e desligado: quem clona o repositorio nao envia evento
    nenhum para lugar nenhum."""
    monkeypatch.setattr(settings, "SENTRY_DSN", None)

    assert configurar_observabilidade() is False


def test_com_dsn_inicializa_uma_vez(monkeypatch: pytest.MonkeyPatch) -> None:
    inits: list[dict] = []
    monkeypatch.setattr(settings, "SENTRY_DSN", "https://chave@sentry.exemplo/42")
    monkeypatch.setattr(observability.sentry_sdk, "init", lambda **kw: inits.append(kw))

    assert configurar_observabilidade() is True
    assert configurar_observabilidade() is True  # create_app() roda por teste
    assert len(inits) == 1

    (config,) = inits
    # PII desligado nao e detalhe: o corpo de um /upload e o documento
    # inteiro de um terceiro.
    assert config["send_default_pii"] is False
    assert config["before_send"] is _filtrar_evento
    # Medido com um coletor local: filtrar cabecalho nao basta. Os locais
    # de cada quadro carregam o `scope` ASGI cru — cabecalhos em bytes, e
    # o texto do documento de quem fez o upload.
    assert config["include_local_variables"] is False
    assert config["environment"] == settings.ENVIRONMENT


# --------------------------------------------------------------------------
# Erro tratado chega ao agregador
# --------------------------------------------------------------------------
async def test_erro_500_e_reportado(monkeypatch: pytest.MonkeyPatch) -> None:
    """O handler de `Exception` impede o erro de subir ate o middleware do
    SDK — sem `capture_exception` explicito, o 500 sairia invisivel."""
    capturadas: list[BaseException] = []
    monkeypatch.setattr(
        observability.sentry_sdk, "capture_exception", lambda exc: capturadas.append(exc)
    )

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/estoura")
    async def estoura() -> None:
        raise RuntimeError("banco sumiu")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resposta = await client.get("/estoura")

    assert resposta.status_code == 500
    # A mensagem do cliente continua generica: o detalhe vai para o
    # agregador, nao para quem chamou.
    assert resposta.json()["detail"] == "Erro interno do servidor."
    assert [str(exc) for exc in capturadas] == ["banco sumiu"]


async def test_erro_de_dominio_nao_vira_evento(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 e 429 sao o funcionamento normal da API. Reportar cada um deles
    encheria o agregador de ruido ate o erro de verdade nao ser visto."""
    from app.core.exceptions import DocumentNotFoundError

    capturadas: list[BaseException] = []
    monkeypatch.setattr(
        observability.sentry_sdk, "capture_exception", lambda exc: capturadas.append(exc)
    )

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/sumiu")
    async def sumiu() -> None:
        raise DocumentNotFoundError()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resposta = await client.get("/sumiu")

    assert resposta.status_code == 404
    assert capturadas == []


# --------------------------------------------------------------------------
# Alerta de custo
# --------------------------------------------------------------------------
class RelatorFalso:
    """Guarda os alertas recebidos, no lugar do envio ao agregador."""

    def __init__(self) -> None:
        self.alertas: list[dict] = []

    def __call__(self, **kwargs: object) -> None:
        self.alertas.append(kwargs)


def limitador(relator: RelatorFalso | None, *, limite: int = 10, limiar: float = 0.8):
    return LimitadorJanelaFixa(
        limite=limite,
        janela_segundos=60,
        escopo="operacoes pagas por dia",
        limiar_de_alerta=limiar,
        relator=relator,
    )


def test_alerta_ao_cruzar_o_limiar() -> None:
    relator = RelatorFalso()
    lim = limitador(relator)

    for _ in range(7):
        lim.registrar("global")
    assert relator.alertas == [], "70% ainda nao alerta"

    lim.registrar("global")  # 8/10 = 80%

    assert len(relator.alertas) == 1
    assert relator.alertas[0]["acessos"] == 8
    assert relator.alertas[0]["esgotado"] is False


def test_limiar_alerta_uma_vez_por_janela() -> None:
    """Um alerta por requisicao seria ruido, e ruido e como um alerta
    deixa de ser lido."""
    relator = RelatorFalso()
    lim = limitador(relator)

    for _ in range(10):
        lim.registrar("global")

    assert len(relator.alertas) == 1


def test_alerta_de_esgotamento_e_separado() -> None:
    relator = RelatorFalso()
    lim = limitador(relator)

    for _ in range(10):
        lim.registrar("global")
    with pytest.raises(Exception):
        lim.registrar("global")

    esgotamento = [a for a in relator.alertas if a["esgotado"]]
    assert len(esgotamento) == 1
    assert esgotamento[0]["acessos"] == 10


def test_esgotamento_alerta_uma_vez_mesmo_com_insistencia() -> None:
    relator = RelatorFalso()
    lim = limitador(relator)

    for _ in range(10):
        lim.registrar("global")
    for _ in range(5):
        with pytest.raises(Exception):
            lim.registrar("global")

    assert len([a for a in relator.alertas if a["esgotado"]]) == 1


def test_limitador_sem_relator_nao_alerta() -> None:
    """E o caso do limitador de rajada: dispara o dia inteiro por motivo
    banal, e alertar sobre ele afogaria o alerta que importa."""
    lim = LimitadorJanelaFixa(limite=2, janela_segundos=60, escopo="requisicoes")

    lim.registrar("cliente")
    lim.registrar("cliente")
    with pytest.raises(Exception):
        lim.registrar("cliente")  # nao levanta AttributeError por relator ausente


def test_falha_do_relator_nao_derruba_a_requisicao() -> None:
    """Quem estava so fazendo um upload legitimo nao pode receber 500
    porque o agregador esta fora do ar."""

    def relator_quebrado(**_: object) -> None:
        raise RuntimeError("sentry fora do ar")

    lim = LimitadorJanelaFixa(
        limite=2,
        janela_segundos=60,
        escopo="operacoes pagas por dia",
        limiar_de_alerta=0.5,
        relator=relator_quebrado,
    )

    lim.registrar("global")  # cruza o limiar e o relator explode
    lim.registrar("global")  # a cota continua sendo contada normalmente


def test_alertar_custo_envia_mensagem(monkeypatch: pytest.MonkeyPatch) -> None:
    mensagens: list[tuple[str, str]] = []
    monkeypatch.setattr(
        observability.sentry_sdk,
        "capture_message",
        lambda msg, level: mensagens.append((msg, level)),
    )

    alertar_custo(escopo="operacoes pagas por dia", acessos=160, limite=200, esgotado=False)
    alertar_custo(escopo="operacoes pagas por dia", acessos=200, limite=200, esgotado=True)

    assert "80%" in mensagens[0][0] and mensagens[0][1] == "warning"
    assert "esgotado" in mensagens[1][0] and mensagens[1][1] == "error"


def test_cabecalho_filtrado_cobre_o_header_real_da_api() -> None:
    """Trava contra renomear o header e esquecer do filtro."""
    assert API_KEY_HEADER_NAME.lower() in observability.CABECALHOS_FILTRADOS
