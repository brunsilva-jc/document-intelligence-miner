"""Extracao de texto (PDF/TXT) e divisao em chunks.

Puro processamento de dados: nao conhece banco, HTTP nem LLM. Recebe
bytes, devolve blocos de texto prontos para vetorizar — o que o torna
trivial de testar.
"""

import hashlib
import io
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING

import anyio
from langchain_text_splitters import RecursiveCharacterTextSplitter

if TYPE_CHECKING:
    from tiktoken import Encoding

from app.core.config import settings
from app.core.exceptions import EmptyDocumentError, FileTooLargeError, UnsupportedFileTypeError
from app.core.logging import get_logger

logger = get_logger(__name__)

# Extensao -> content types aceitos. A extensao e a fonte de verdade:
# navegadores e clientes HTTP mentem com frequencia no Content-Type.
SUPPORTED_EXTENSIONS: dict[str, tuple[str, ...]] = {
    ".pdf": ("application/pdf",),
    ".txt": ("text/plain",),
    ".md": ("text/markdown", "text/plain"),
}

_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


@dataclass(slots=True)
class TextChunk:
    """Bloco de texto pronto para virar um `DocumentChunk`."""

    index: int
    content: str
    token_count: int | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class ExtractedText:
    """Texto extraido de um arquivo, com suas paginas preservadas."""

    # (numero_da_pagina, texto). Arquivos texto viram uma unica "pagina".
    pages: list[tuple[int, str]]

    @property
    def full_text(self) -> str:
        return "\n\n".join(text for _, text in self.pages)


class DocumentProcessor:
    """Converte bytes de um arquivo em chunks de texto."""

    def __init__(
        self,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        max_size_bytes: int | None = None,
    ) -> None:
        # `is None` e nao `or`: overlap 0 e uma escolha valida, e `or`
        # silenciosamente a trocaria pelo padrao das settings.
        self._chunk_size = chunk_size if chunk_size is not None else settings.CHUNK_SIZE
        self._chunk_overlap = chunk_overlap if chunk_overlap is not None else settings.CHUNK_OVERLAP
        self._max_size_bytes = (
            max_size_bytes if max_size_bytes is not None else settings.MAX_UPLOAD_SIZE_BYTES
        )
        # Separadores em ordem de preferencia: quebra por paragrafo antes
        # de quebrar por linha, e por linha antes de partir uma palavra.
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=self._chunk_size,
            chunk_overlap=self._chunk_overlap,
            separators=["\n\n", "\n", ". ", " ", ""],
            length_function=len,
            keep_separator=True,
        )

    # ------------------------------------------------------------------
    # Validacao
    # ------------------------------------------------------------------
    def validate(self, filename: str, content_type: str | None, size_bytes: int) -> str:
        """Valida extensao, content type e tamanho. Retorna a extensao."""
        extension = self._extension_of(filename)

        if extension not in SUPPORTED_EXTENSIONS:
            supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
            raise UnsupportedFileTypeError(
                f"Extensao '{extension or filename}' nao suportada. Aceitos: {supported}."
            )

        allowed_types = SUPPORTED_EXTENSIONS[extension]
        normalized = (content_type or "").split(";")[0].strip().lower()
        # `application/octet-stream` e o fallback generico de muitos
        # clientes; a extensao ja foi validada, entao aceitamos.
        if normalized and normalized not in allowed_types + ("application/octet-stream",):
            raise UnsupportedFileTypeError(
                f"Content-Type '{normalized}' incompativel com a extensao '{extension}'."
            )

        if size_bytes <= 0:
            raise EmptyDocumentError("Arquivo vazio.")

        if size_bytes > self._max_size_bytes:
            size_mb = size_bytes / (1024 * 1024)
            limit_mb = self._max_size_bytes / (1024 * 1024)
            raise FileTooLargeError(
                f"Arquivo de {size_mb:.1f} MB excede o limite de {limit_mb:.0f} MB."
            )

        return extension

    @staticmethod
    def _extension_of(filename: str) -> str:
        _, dot, extension = filename.rpartition(".")
        return f".{extension.lower()}" if dot else ""

    @staticmethod
    def checksum(data: bytes) -> str:
        """SHA-256 do arquivo, usado para deduplicar reenvios."""
        return hashlib.sha256(data).hexdigest()

    # ------------------------------------------------------------------
    # Extracao
    # ------------------------------------------------------------------
    async def extract(self, data: bytes, extension: str) -> ExtractedText:
        """Extrai texto do arquivo, fora do event loop (I/O e CPU bound)."""
        if extension == ".pdf":
            extracted = await anyio.to_thread.run_sync(self._extract_pdf, data)
        else:
            extracted = await anyio.to_thread.run_sync(self._extract_plain_text, data)

        if not extracted.full_text.strip():
            raise EmptyDocumentError(
                "Nenhum texto extraido. PDFs digitalizados exigem OCR, ainda nao suportado."
            )
        return extracted

    def _extract_pdf(self, data: bytes) -> ExtractedText:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                # Tenta a senha vazia, comum em PDFs apenas "protegidos".
                try:
                    reader.decrypt("")
                except Exception as exc:
                    raise EmptyDocumentError("PDF protegido por senha.") from exc

            pages: list[tuple[int, str]] = []
            for number, page in enumerate(reader.pages, start=1):
                text = self._normalize(page.extract_text() or "")
                if text:
                    pages.append((number, text))
        except PdfReadError as exc:
            raise EmptyDocumentError(f"PDF invalido ou corrompido: {exc}") from exc

        return ExtractedText(pages=pages)

    def _extract_plain_text(self, data: bytes) -> ExtractedText:
        for encoding in ("utf-8", "utf-16", "latin-1"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:  # pragma: no cover - latin-1 aceita qualquer byte
            raise EmptyDocumentError("Nao foi possivel decodificar o arquivo como texto.")

        return ExtractedText(pages=[(1, self._normalize(text))])

    @staticmethod
    def _normalize(text: str) -> str:
        """Colapsa espacos e linhas em branco excessivos do PDF."""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = _WHITESPACE_RE.sub(" ", text)
        text = _BLANK_LINES_RE.sub("\n\n", text)
        return "\n".join(line.strip() for line in text.split("\n")).strip()

    # ------------------------------------------------------------------
    # Chunking
    # ------------------------------------------------------------------
    def chunk(self, extracted: ExtractedText) -> list[TextChunk]:
        """Divide pagina a pagina, preservando a origem de cada bloco.

        Dividir por pagina (em vez do texto concatenado) mantem a citacao
        precisa e evita blocos que misturam paginas distantes.
        """
        chunks: list[TextChunk] = []
        for page_number, page_text in extracted.pages:
            for piece in self._splitter.split_text(page_text):
                content = piece.strip()
                if not content:
                    continue
                chunks.append(
                    TextChunk(
                        index=len(chunks),
                        content=content,
                        token_count=count_tokens(content),
                        metadata={"page": page_number},
                    )
                )

        if not chunks:
            raise EmptyDocumentError("O documento nao produziu nenhum bloco de texto.")

        logger.info(
            "documento dividido em %d chunks (size=%d, overlap=%d)",
            len(chunks),
            self._chunk_size,
            self._chunk_overlap,
        )
        return chunks


@lru_cache(maxsize=1)
def _encoding() -> "Encoding | None":
    """Encoder do tiktoken, carregado uma vez (baixa o vocabulario)."""
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception as exc:  # pragma: no cover - rede/pacote indisponivel
        logger.warning("tiktoken indisponivel, token_count ficara nulo: %s", exc)
        return None


def count_tokens(text: str) -> int | None:
    """Conta tokens do bloco; `None` quando o tiktoken nao esta disponivel.

    E metadado de observabilidade (custo/limite de contexto), nunca uma
    dependencia do pipeline — por isso a falha e degradacao, nao erro.
    """
    encoding = _encoding()
    return len(encoding.encode(text)) if encoding is not None else None
