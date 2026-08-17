from __future__ import annotations

import io

from google.genai import types
from pypdf import PdfReader

from app.config import Settings
from app.gemini import build_client
from app.models import Resume

MIN_PHOTO_DIMENSION = 100

SYSTEM_PROMPT = """You are extracting structured resume data from raw text that was mechanically
extracted from a PDF resume (so spacing, line breaks, and column order may be jumbled).

Organize the content into the given schema as faithfully as possible:
- Only include information that is actually present in the source text.
- Do NOT invent employers, titles, dates, locations, degrees, institutions, skills, or technologies
  that aren't in the source text.
- If the source resume doesn't group its skills into categories, put them all under a single
  category with an empty category name.
- If a field isn't present in the source (e.g. no LinkedIn URL), leave it blank rather than guessing.
- Preserve the original wording of bullet points, summary, etc. as closely as possible — this is an
  extraction/structuring task, not a rewrite.
"""


class PdfImportError(Exception):
    pass


_LIGATURES = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
}


def _normalize_ligatures(text: str) -> str:
    for ligature, expansion in _LIGATURES.items():
        text = text.replace(ligature, expansion)
    return text


def extract_text_from_pdf(file_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise PdfImportError(f"Could not read PDF file: {exc}") from exc

    text = _normalize_ligatures("\n".join(pages)).strip()
    if not text:
        raise PdfImportError(
            "No extractable text found in this PDF (it may be a scanned image without OCR)."
        )
    return text


def extract_photo_from_pdf(file_bytes: bytes) -> bytes | None:
    """Best-effort: find the largest embedded raster image across all pages (assumed to be a
    headshot, since that's typically the largest photo-like image on a resume) and return it
    re-encoded as JPEG. Returns None if the PDF has no images worth using as a photo — this is
    a heuristic, not a guarantee, so callers should let the user replace it manually if wrong."""
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
    except Exception:
        return None

    candidates = []
    for page in reader.pages:
        try:
            images = page.images
        except Exception:
            continue
        for img in images:
            try:
                pil_image = img.image
            except Exception:
                pil_image = None
            if pil_image is None:
                continue
            width, height = pil_image.size
            if width < MIN_PHOTO_DIMENSION or height < MIN_PHOTO_DIMENSION:
                continue
            candidates.append((width * height, pil_image))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0], reverse=True)
    _, best_image = candidates[0]

    buf = io.BytesIO()
    best_image.convert("RGB").save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def structure_resume_from_text(raw_text: str, settings: Settings) -> Resume:
    client = build_client(settings)
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=f"Raw resume text extracted from a PDF:\n\n{raw_text}",
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=Resume,
        ),
    )
    resume = response.parsed
    if resume is None:
        raise PdfImportError("Gemini response could not be parsed into the resume schema")
    return resume
