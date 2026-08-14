"""Testes de extracao, validacao e chunking."""

import pytest

from app.core.exceptions import EmptyDocumentError, FileTooLargeError, UnsupportedFileTypeError
from app.services.document_processor import DocumentProcessor
from tests.pdf_fixture import make_pdf

MINIMAL_PDF = make_pdf(["Prazo de 24 meses"])


@pytest.fixture
def processor() -> DocumentProcessor:
    return DocumentProcessor(chunk_size=100, chunk_overlap=20)


# ----------------------------------------------------------------------
# Validacao
# ----------------------------------------------------------------------
def test_accepts_supported_extensions(processor: DocumentProcessor) -> None:
    assert processor.validate("nota.pdf", "application/pdf", 1000) == ".pdf"
    assert processor.validate("README.md", "text/markdown", 10) == ".md"
    # Content-Type generico e aceito: a extensao ja foi validada.
    assert processor.validate("a.txt", "application/octet-stream", 10) == ".txt"


def test_rejects_unsupported_extension(processor: DocumentProcessor) -> None:
    with pytest.raises(UnsupportedFileTypeError):
        processor.validate("planilha.xlsx", "application/vnd.ms-excel", 100)


def test_rejects_content_type_mismatch(processor: DocumentProcessor) -> None:
    with pytest.raises(UnsupportedFileTypeError):
        processor.validate("doc.pdf", "image/png", 100)


def test_rejects_empty_and_oversized_files() -> None:
    processor = DocumentProcessor(max_size_bytes=1024)

    with pytest.raises(EmptyDocumentError):
        processor.validate("a.txt", "text/plain", 0)

    with pytest.raises(FileTooLargeError):
        processor.validate("a.txt", "text/plain", 2048)


def test_checksum_is_stable_and_content_addressed() -> None:
    assert DocumentProcessor.checksum(b"abc") == DocumentProcessor.checksum(b"abc")
    assert DocumentProcessor.checksum(b"abc") != DocumentProcessor.checksum(b"abd")


# ----------------------------------------------------------------------
# Extracao
# ----------------------------------------------------------------------
async def test_extracts_plain_text(processor: DocumentProcessor) -> None:
    extracted = await processor.extract("Linha 1\r\n\r\n\r\n\r\nLinha 2".encode(), ".txt")

    assert extracted.pages == [(1, "Linha 1\n\nLinha 2")]  # linhas em branco colapsadas


async def test_extracts_pdf_text_with_page_numbers(processor: DocumentProcessor) -> None:
    extracted = await processor.extract(MINIMAL_PDF, ".pdf")

    assert len(extracted.pages) == 1
    page_number, text = extracted.pages[0]
    assert page_number == 1
    assert "24 meses" in text


async def test_rejects_document_without_extractable_text(
    processor: DocumentProcessor,
) -> None:
    with pytest.raises(EmptyDocumentError):
        await processor.extract(b"   \n  \n ", ".txt")


async def test_rejects_corrupted_pdf(processor: DocumentProcessor) -> None:
    with pytest.raises(EmptyDocumentError):
        await processor.extract(b"nao sou um pdf", ".pdf")


# ----------------------------------------------------------------------
# Chunking
# ----------------------------------------------------------------------
async def test_chunks_are_indexed_and_bounded(processor: DocumentProcessor) -> None:
    text = ". ".join(f"Frase numero {i} com algum conteudo" for i in range(40))

    extracted = await processor.extract(text.encode(), ".txt")
    chunks = processor.chunk(extracted)

    assert len(chunks) > 1
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    # O splitter respeita o teto, exceto por separadores mantidos na borda.
    assert all(len(chunk.content) <= 120 for chunk in chunks)
    assert all(chunk.content == chunk.content.strip() for chunk in chunks)


async def test_chunk_keeps_source_page_in_metadata(processor: DocumentProcessor) -> None:
    pdf = make_pdf(["Texto da primeira pagina", "Texto da segunda pagina"])

    extracted = await processor.extract(pdf, ".pdf")
    chunks = processor.chunk(extracted)

    # Cada chunk carrega a pagina de onde saiu, base para a citacao.
    assert [chunk.metadata["page"] for chunk in chunks] == [1, 2]
    assert "primeira" in chunks[0].content
    assert "segunda" in chunks[1].content


async def test_short_document_yields_single_chunk(processor: DocumentProcessor) -> None:
    extracted = await processor.extract(b"Documento curto.", ".txt")

    chunks = processor.chunk(extracted)

    assert len(chunks) == 1
    assert chunks[0].content == "Documento curto."
