"""Shared plumbing for the integration tests.

These tests drive the real FastAPI application through ``TestClient`` -- real routing, real
request validation, real Jinja2 rendering, real resume parsing, real LaTeX and .docx rendering.
Only three things are faked, and only because they are network or credential bound:

* ``app.db``   -- replaced by :class:`FakeDb`, an in-memory stand-in for Supabase.
* the Gemini calls -- replaced by deterministic stubs.
* authentication -- replaced via FastAPI's ``dependency_overrides``.

The point is to answer "does the app still work end to end", which the unit tests cannot: no
existing test imports ``app.main`` at all.
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db as app_db
from app import main as app_main
from app.auth import get_current_user_id
from app.models import CoverLetter
from app.resume_parser import parse_resume_text

TEST_USER = "test-user-1"
OTHER_USER = "test-user-2"

SAMPLE_RESUME_MD = (
    Path(__file__).parent.parent / "fixtures" / "sample_resume.md"
).read_text(encoding="utf-8")


class FakeDb:
    """In-memory stand-in for :mod:`app.db`.

    Mirrors the observable behaviour the routes actually depend on, including the quirks: a
    missing resume row yields ``""`` rather than ``None``, ``get_job`` scopes by user id (that's
    the authorisation check for job access), and ``update_job`` merges keyword fields into the
    existing row rather than replacing it.
    """

    def __init__(self) -> None:
        self.resumes: dict[str, str] = {}
        self.photos: dict[str, bytes] = {}
        self.photo_content_types: dict[str, str] = {}
        self.jobs: dict[str, dict] = {}
        self.usage: dict[str, int] = {}
        self.documents: dict[str, bytes] = {}

    # --- resume ---------------------------------------------------------------------------

    def get_resume_markdown(self, user_id: str) -> str:
        return self.resumes.get(user_id, "")

    def save_resume_markdown(self, user_id: str, markdown_text: str) -> None:
        self.resumes[user_id] = markdown_text

    # --- photo ----------------------------------------------------------------------------

    def get_photo_bytes(self, user_id: str) -> bytes | None:
        return self.photos.get(user_id)

    def save_photo(self, user_id: str, content: bytes, content_type: str = "image/jpeg") -> None:
        self.photos[user_id] = content
        self.photo_content_types[user_id] = content_type

    def delete_photo(self, user_id: str) -> None:
        self.photos.pop(user_id, None)
        self.photo_content_types.pop(user_id, None)

    # --- generated documents --------------------------------------------------------------

    def upload_document(
        self,
        user_id: str,
        filename: str,
        content: bytes,
        content_type: str = "application/pdf",
    ) -> str:
        storage_path = f"{user_id}/{filename}"
        self.documents[storage_path] = content
        return storage_path

    def create_signed_document_url(self, storage_path: str, expires_in: int = 3600) -> str:
        return f"https://storage.test/{storage_path}?token=signed&expires={expires_in}"

    # --- jobs -----------------------------------------------------------------------------

    def create_job(self, user_id: str) -> str:
        job_id = str(uuid.uuid4())
        self.jobs[job_id] = {
            "id": job_id,
            "user_id": user_id,
            "status": "running",
            "stage": "Starting...",
            "percent": 0,
        }
        return job_id

    def update_job(self, job_id: str, **fields) -> None:
        self.jobs[job_id].update(fields)

    def get_job(self, job_id: str, user_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        if job is None or job["user_id"] != user_id:
            return None
        # A copy, because the /jobs/{id} route mutates what it gets back (adding pdf_url and
        # cover_letter_url) before returning it -- the real implementation hands back a fresh
        # dict decoded from the API response, so handing back the live one would let a route
        # quietly write into our stored state.
        return dict(job)

    # --- usage ----------------------------------------------------------------------------

    def get_daily_usage(self, user_id: str) -> int:
        return self.usage.get(user_id, 0)

    def increment_usage(self, user_id: str) -> int:
        self.usage[user_id] = self.usage.get(user_id, 0) + 1
        return self.usage[user_id]


_DB_FUNCTIONS = (
    "get_resume_markdown",
    "save_resume_markdown",
    "get_photo_bytes",
    "save_photo",
    "delete_photo",
    "upload_document",
    "create_signed_document_url",
    "create_job",
    "update_job",
    "get_job",
    "get_daily_usage",
    "increment_usage",
)


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> FakeDb:
    """Swap every app.db function the routes use for a FakeDb bound method.

    Patching attributes on the app.db *module* is what makes this work: app/main.py does
    `from app import db` and then calls `db.get_resume_markdown(...)`, so the lookup happens on
    the module object at call time and sees the replacement.
    """
    fake = FakeDb()
    for name in _DB_FUNCTIONS:
        assert hasattr(app_db, name), f"app.db has no {name!r} -- did the module get refactored?"
        monkeypatch.setattr(app_db, name, getattr(fake, name))
    return fake


@pytest.fixture
def stub_gemini(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace the three Gemini-backed calls with deterministic stand-ins.

    These are patched on ``app.main``, *not* on app.tailor / app.cover_letter / app.pdf_import.
    main.py imports them by name (``from app.tailor import tailor_resume``), so the name it
    actually calls is ``app.main.tailor_resume``; patching the defining module would leave
    main's own reference untouched and the test would silently call the real Gemini API --
    burning quota, needing a key, and failing offline.
    """
    calls: dict[str, object] = {}

    def fake_tailor_resume(resume, jd_text, settings):
        calls["tailor_resume"] = jd_text
        # Returning a modified copy keeps the "identity comes from the resume, not the model"
        # invariant that the real tailor preserves.
        return resume.model_copy(update={"summary": f"Tailored summary for: {jd_text[:40]}"})

    def fake_generate_cover_letter(resume, jd_text, settings):
        calls["generate_cover_letter"] = jd_text
        return CoverLetter(
            company="Acme Corp",
            role="Senior Software Engineer",
            paragraphs=["First paragraph.", "Second paragraph."],
        )

    def fake_structure_resume_from_text(raw_text, settings):
        calls["structure_resume_from_text"] = raw_text
        return parse_resume_text(SAMPLE_RESUME_MD)

    monkeypatch.setattr(app_main, "tailor_resume", fake_tailor_resume)
    monkeypatch.setattr(app_main, "generate_cover_letter", fake_generate_cover_letter)
    monkeypatch.setattr(app_main, "structure_resume_from_text", fake_structure_resume_from_text)
    return calls


@pytest.fixture
def client(fake_db: FakeDb, stub_gemini: dict):
    """An authenticated TestClient. Every request behaves as TEST_USER."""
    app_main.app.dependency_overrides[get_current_user_id] = lambda: TEST_USER
    try:
        with TestClient(app_main.app) as test_client:
            yield test_client
    finally:
        app_main.app.dependency_overrides.clear()


@pytest.fixture
def anon_client(fake_db: FakeDb):
    """An unauthenticated TestClient, for asserting the 401 paths."""
    app_main.app.dependency_overrides.clear()
    with TestClient(app_main.app) as test_client:
        yield test_client


@pytest.fixture
def jpeg_bytes() -> bytes:
    """A small valid JPEG, generated rather than committed (see tests/conftest.py)."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (200, 260), (180, 180, 190)).save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


@pytest.fixture
def blank_pdf_bytes() -> bytes:
    """A structurally valid PDF with a page but no text, for the import-failure path."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
