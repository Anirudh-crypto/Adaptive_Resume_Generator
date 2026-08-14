import pytest

from app.pdf_import import (
    PdfImportError,
    _normalize_ligatures,
    extract_photo_from_pdf,
    extract_text_from_pdf,
)

BLANK_PDF = (
    b"%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>"
)


def test_normalize_ligatures():
    assert _normalize_ligatures("workﬂows and oﬃce") == "workflows and office"


def test_extract_text_from_invalid_pdf_raises():
    with pytest.raises(PdfImportError):
        extract_text_from_pdf(b"not a real pdf")


def test_extract_text_from_empty_pdf_raises():
    # A minimal, syntactically valid single blank-page PDF with no text content.
    with pytest.raises(PdfImportError):
        extract_text_from_pdf(BLANK_PDF)


def test_extract_photo_from_pdf_with_no_images_returns_none():
    assert extract_photo_from_pdf(BLANK_PDF) is None


def test_extract_photo_from_invalid_pdf_returns_none():
    assert extract_photo_from_pdf(b"not a real pdf") is None
