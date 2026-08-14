"""Gerador de PDFs validos para os testes.

Escrever o PDF na mao (com xref correto) evita adicionar uma dependencia
de geracao — reportlab/fpdf — so para os testes.
"""


def _escape(text: str) -> bytes:
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return escaped.encode("latin-1", errors="replace")


def make_pdf(pages: list[str]) -> bytes:
    """Monta um PDF de uma linha de texto por pagina."""
    page_count = len(pages)
    page_ids = [3 + 2 * i for i in range(page_count)]
    content_ids = [4 + 2 * i for i in range(page_count)]
    font_id = 3 + 2 * page_count

    objects: list[bytes] = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids["
        + b" ".join(b"%d 0 R" % pid for pid in page_ids)
        + b"]/Count %d>>" % page_count,
    ]

    for page_id, content_id, text in zip(page_ids, content_ids, pages, strict=True):
        del page_id  # a ordem na lista ja define o numero do objeto
        objects.append(
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
            b"/Resources<</Font<</F1 %d 0 R>>>>/Contents %d 0 R>>" % (font_id, content_id)
        )
        stream = b"BT /F1 12 Tf 72 720 Td (" + _escape(text) + b") Tj ET"
        objects.append(b"<</Length %d>>\nstream\n" % len(stream) + stream + b"\nendstream")

    objects.append(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")

    # Serializa acumulando os offsets exigidos pela tabela xref.
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"

    xref_offset = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset

    out += b"trailer\n<</Size %d/Root 1 0 R>>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref_offset
    return bytes(out)
