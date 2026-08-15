"""Tests for hive-mcp's tools/web.py -- web_search, web_fetch, and the 2026-08-15
PDF extraction fix.

Root cause this fixes: web_fetch's non-HTML branch was `resp.text[:max_chars]` for
EVERYTHING that wasn't text/html -- for a PDF (content-type application/pdf, or a
server that sends application/octet-stream for one), that decodes raw PDF binary
structure as text, producing unusable garbage silently (no error, no warning --
just a string of mojibake that looks superficially like "a result"). Directly
blocks the "search sites and files/pdfs to gather context" workflow the planning/
research teams are meant to support.
"""
import io

import pytest

from tools import web


def _make_minimal_pdf(text: str) -> bytes:
    """Hand-build a minimal, valid single-page PDF with one text line -- avoids a
    dependency on a PDF-writing library (pypdf itself is a reader/merger, not a
    from-scratch text-drawing writer) while producing bytes real enough for pypdf's
    own extract_text() to read back correctly, with a correct xref table (not
    relying on pypdf's lenient-parsing recovery path)."""
    objects = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
    )
    stream_content = f"BT /F1 18 Tf 10 150 Td ({text}) Tj ET".encode("latin-1")
    objects.append(
        b"<< /Length " + str(len(stream_content)).encode() + b" >>\nstream\n"
        + stream_content + b"\nendstream"
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]  # object 0 is free, offset unused
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode())
        out.write(body)
        out.write(b"\nendobj\n")

    xref_offset = out.tell()
    n = len(objects) + 1
    out.write(f"xref\n0 {n}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(b"trailer\n")
    out.write(f"<< /Size {n} /Root 1 0 R >>\n".encode())
    out.write(b"startxref\n")
    out.write(f"{xref_offset}\n".encode())
    out.write(b"%%EOF")
    return out.getvalue()


def _make_blank_pdf() -> bytes:
    """A page with no /Contents stream at all -- exercises the 'no extractable
    text' path without needing an actually-empty-but-valid content stream."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode())
        out.write(body)
        out.write(b"\nendobj\n")
    xref_offset = out.tell()
    n = len(objects) + 1
    out.write(f"xref\n0 {n}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(b"trailer\n")
    out.write(f"<< /Size {n} /Root 1 0 R >>\n".encode())
    out.write(b"startxref\n")
    out.write(f"{xref_offset}\n".encode())
    out.write(b"%%EOF")
    return out.getvalue()


# ── _extract_pdf_text ────────────────────────────────────────────────────────────

def test_extracts_real_text_from_a_valid_pdf():
    pdf_bytes = _make_minimal_pdf("Hello PDF World")

    result = web._extract_pdf_text(pdf_bytes, max_chars=8000)

    assert "Hello PDF World" in result


def test_truncates_to_max_chars():
    pdf_bytes = _make_minimal_pdf("Hello PDF World")

    result = web._extract_pdf_text(pdf_bytes, max_chars=5)

    assert len(result) <= 5


def test_corrupt_pdf_bytes_return_a_clear_message_not_a_crash():
    result = web._extract_pdf_text(b"this is not a pdf at all", max_chars=8000)

    assert "Could not open PDF" in result


def test_pdf_with_no_text_content_returns_a_clear_message():
    pdf_bytes = _make_blank_pdf()

    result = web._extract_pdf_text(pdf_bytes, max_chars=8000)

    assert "no extractable text" in result.lower()


def test_pypdf_missing_returns_a_clear_message_not_a_crash(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("no module named pypdf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    result = web._extract_pdf_text(b"anything", max_chars=8000)

    assert "pypdf not installed" in result


# ── web_fetch: PDF routing ───────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, content: bytes, content_type: str):
        self.content = content
        self.text = content.decode("latin-1")
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        pass


@pytest.fixture(autouse=True)
def _enable_web_search(monkeypatch):
    monkeypatch.setattr(web.config, "WEB_SEARCH_ENABLED", True)


def test_web_fetch_routes_application_pdf_content_type_to_pdf_extraction(monkeypatch):
    pdf_bytes = _make_minimal_pdf("From content-type")

    def fake_get(url, **kwargs):
        return _FakeResponse(pdf_bytes, "application/pdf")

    monkeypatch.setattr("httpx.get", fake_get)

    result = web.web_fetch("https://example.com/doc")

    assert "From content-type" in result


def test_web_fetch_routes_pdf_extension_to_pdf_extraction_even_with_octet_stream(monkeypatch):
    """Some servers mislabel a PDF as application/octet-stream -- the .pdf
    extension in the URL must still route correctly, not fall through to the
    generic branch and return decoded-binary garbage."""
    pdf_bytes = _make_minimal_pdf("From extension")

    def fake_get(url, **kwargs):
        return _FakeResponse(pdf_bytes, "application/octet-stream")

    monkeypatch.setattr("httpx.get", fake_get)

    result = web.web_fetch("https://example.com/reports/spec.pdf")

    assert "From extension" in result


def test_web_fetch_pdf_extension_with_query_string_still_routes_correctly(monkeypatch):
    pdf_bytes = _make_minimal_pdf("Query string case")

    def fake_get(url, **kwargs):
        return _FakeResponse(pdf_bytes, "application/octet-stream")

    monkeypatch.setattr("httpx.get", fake_get)

    result = web.web_fetch("https://example.com/spec.pdf?version=2")

    assert "Query string case" in result


def test_web_fetch_html_is_unaffected_by_the_pdf_change(monkeypatch):
    def fake_get(url, **kwargs):
        return _FakeResponse(b"<html><body><main>Real content here</main></body></html>", "text/html")

    monkeypatch.setattr("httpx.get", fake_get)

    result = web.web_fetch("https://example.com/page")

    assert "Real content here" in result
