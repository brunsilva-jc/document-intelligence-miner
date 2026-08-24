"""Teto de tamanho do corpo, aplicado ANTES de o corpo ser lido.

`DocumentProcessor.validate` ja recusa arquivo grande demais, mas tarde:
quando a rota roda, o FastAPI ja recebeu e montou o multipart inteiro. O
413 sai correto e mesmo assim os bytes ja passaram pela memoria e pelo
disco do container — que e exatamente o que se queria evitar numa maquina
com teto de memoria.

Este middleware corta antes, em duas frentes:

1. **`Content-Length` declarado** — resposta imediata, sem ler um byte.
   Cobre o cliente honesto, que e o caso comum.
2. **Contagem do que chega** — para corpo sem `Content-Length`
   (`Transfer-Encoding: chunked`) ou com tamanho mentido, corta assim que
   o acumulado passa do teto.

No segundo caso o corte NAO levanta excecao: quem esta lendo o corpo e o
parser de multipart, e ele traduz qualquer erro seu para um 400
generico — a recusa chegaria ao cliente com o codigo errado. Em vez
disso o middleware faz duas coisas sem drama: encerra o corpo (o parser
recebe um fim de stream e para de pedir bytes, o que ja interrompe o
envio) e substitui a resposta que o app produzir pelo 413. O codigo final
passa a nao depender de como o parser reage a um corpo truncado.

Nada disso substitui o limite no proxy reverso (veja `docs/DEPLOY.md`):
so o proxy evita que os bytes atravessem a rede ate aqui. Este middleware
e a segunda linha, para quando a aplicacao roda sem proxy na frente ou o
limite de la for esquecido.
"""

import json

from fastapi import status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.exceptions import FileTooLargeError
from app.core.logging import get_logger

logger = get_logger(__name__)

# Folga sobre o limite do arquivo: o multipart carrega fronteiras, nomes
# de campo e cabecalhos alem do conteudo. Sem a folga, um arquivo de
# exatamente MAX_UPLOAD_SIZE_MB seria recusado pelo enquadramento — e com
# a mensagem errada, que fala de tamanho de arquivo.
MULTIPART_OVERHEAD_BYTES = 1024 * 1024


def _mensagem(recebido: int, limite: int) -> str:
    return (
        f"Corpo da requisicao com {recebido / (1024 * 1024):.1f} MB excede o "
        f"limite de {limite / (1024 * 1024):.0f} MB."
    )


async def _responder_413(send: Send, recebido: int, limite: int) -> None:
    corpo = json.dumps(
        {"detail": _mensagem(recebido, limite), "error": FileTooLargeError.__name__}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(corpo)).encode()),
                # A resposta sai com corpo por ler (ou por chegar); sem
                # fechar, o resto seria interpretado como a requisicao
                # seguinte na mesma conexao.
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": corpo})


class _CorpoVigiado:
    """Envolve o par `receive`/`send` de UMA requisicao.

    Guarda o estado que os dois lados precisam compartilhar: quantos
    bytes ja passaram e se o teto foi estourado.
    """

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._recebido = 0
        self._excedido = False
        self._respondido = False

    def receive(self, receive: Receive) -> Receive:
        async def receive_contado() -> Message:
            mensagem = await receive()
            if mensagem["type"] != "http.request":
                return mensagem

            self._recebido += len(mensagem.get("body", b""))
            if not self._excedido and self._recebido > self._max_bytes:
                self._excedido = True
                logger.warning(
                    "corpo cortado no meio do envio: %d bytes (teto %d)",
                    self._recebido,
                    self._max_bytes,
                )

            if self._excedido:
                # Fim de stream: o parser para de pedir bytes e o cliente
                # para de enviar. O conteudo ja lido e descartado com a
                # resposta que o app venha a produzir.
                return {"type": "http.request", "body": b"", "more_body": False}
            return mensagem

        return receive_contado

    def send(self, send: Send) -> Send:
        async def send_filtrado(mensagem: Message) -> None:
            if not self._excedido:
                await send(mensagem)
                return

            # O app respondeu a partir de um corpo truncado: qualquer que
            # seja essa resposta, ela e descartada em favor do 413.
            if mensagem["type"] == "http.response.start" and not self._respondido:
                self._respondido = True
                await _responder_413(send, self._recebido, self._max_bytes)

        return send_filtrado


class LimiteDeCorpoMiddleware:
    """Middleware ASGI puro: precisa do controle dos canais da requisicao."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declarado = self._content_length(scope)
        if declarado is not None and declarado > self._max_bytes:
            logger.warning(
                "corpo recusado antes da leitura: %d bytes declarados (teto %d)",
                declarado,
                self._max_bytes,
            )
            await _responder_413(send, declarado, self._max_bytes)
            return

        vigiado = _CorpoVigiado(self._max_bytes)
        await self.app(scope, vigiado.receive(receive), vigiado.send(send))

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        for nome, valor in scope.get("headers", []):
            if nome == b"content-length":
                try:
                    return int(valor)
                except ValueError:
                    # Cabecalho malformado: nao da para confiar no numero,
                    # entao sobra a contagem do que chega de fato.
                    return None
        return None
