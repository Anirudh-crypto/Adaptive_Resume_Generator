"""The generation pipeline, driven synchronously.

``POST /generate`` hands ``_run_pipeline`` to a daemon thread and returns 202 immediately. Tests
call ``_run_pipeline`` directly instead of going through the route: no thread to race, no
arbitrary sleep, and a failure points at the pipeline rather than at scheduling.

Everything from the LaTeX render onwards is the real thing -- real Jinja2 -> .tex, a real
Tectonic subprocess producing a real PDF, real python-docx. Only Gemini and Supabase are faked,
so these tests cover the parts of the pipeline that actually break.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app import db as app_db
from app import main as app_main
from app.tailor import TailoringMismatchError

from .conftest import SAMPLE_RESUME_MD, TEST_USER

pytestmark = pytest.mark.integration

JOB_DESCRIPTION = "Senior Backend Engineer working on distributed systems in Python."


def run_pipeline(fake_db, *, resume_markdown: str | None = SAMPLE_RESUME_MD) -> dict:
    """Create a job, run the pipeline to completion, and return the final job row."""
    if resume_markdown is not None:
        fake_db.resumes[TEST_USER] = resume_markdown
    job_id = fake_db.create_job(TEST_USER)
    app_main._run_pipeline(job_id, TEST_USER, JOB_DESCRIPTION)
    return fake_db.jobs[job_id]


# --- happy paths ---------------------------------------------------------------------------


@pytest.mark.tectonic
def test_pipeline_produces_a_pdf_and_a_cover_letter(fake_db, stub_gemini):
    job = run_pipeline(fake_db)

    assert job["status"] == "done"
    assert job["percent"] == 100
    assert job["stage"] == "Done"
    assert job.get("error") is None
    assert job["cover_letter_error"] is None

    pdf = fake_db.documents[job["pdf_storage_path"]]
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 1000, "a one-page resume PDF should not be a stub"

    docx = fake_db.documents[job["cover_letter_storage_path"]]
    assert docx[:2] == b"PK", ".docx is a zip container"
    with zipfile.ZipFile(io.BytesIO(docx)) as archive:
        assert "word/document.xml" in archive.namelist()

    # Both Gemini calls received the job description, not a truncated or empty string.
    assert stub_gemini["tailor_resume"] == JOB_DESCRIPTION
    assert stub_gemini["generate_cover_letter"] == JOB_DESCRIPTION


@pytest.mark.tectonic
def test_pipeline_embeds_the_photo_when_one_is_saved(fake_db, stub_gemini, jpeg_bytes):
    """The photo travels as a compile resource, so this exercises the has_photo template branch
    and the resource-writing path in compile_pdf together."""
    fake_db.photos[TEST_USER] = jpeg_bytes
    job = run_pipeline(fake_db)

    assert job["status"] == "done"
    assert fake_db.documents[job["pdf_storage_path"]][:4] == b"%PDF"


@pytest.mark.tectonic
def test_pipeline_reports_progress_in_order(fake_db, stub_gemini, monkeypatch):
    """Progress must be monotonic -- the UI polls this and would jump backwards otherwise."""
    percents: list[int] = []
    original_update = fake_db.update_job

    def recording_update(job_id, **fields):
        if "percent" in fields:
            percents.append(fields["percent"])
        original_update(job_id, **fields)

    # Patch app.db, not the FakeDb instance: the fake_db fixture already pointed
    # app.db.update_job at the instance's bound method, so reassigning the attribute on the
    # instance would leave the pipeline still calling the original and record nothing.
    monkeypatch.setattr(app_db, "update_job", recording_update)
    run_pipeline(fake_db)

    assert percents == sorted(percents)
    assert percents[-1] == 100


# --- failure paths -------------------------------------------------------------------------


def test_pipeline_errors_when_no_resume_is_saved(fake_db, stub_gemini):
    job = run_pipeline(fake_db, resume_markdown="")

    assert job["status"] == "error"
    assert "Could not parse resume" in job["error"]
    assert "pdf_storage_path" not in job


def test_pipeline_errors_on_an_unparseable_resume(fake_db, stub_gemini):
    job = run_pipeline(fake_db, resume_markdown="no heading here")

    assert job["status"] == "error"
    assert "Could not parse resume" in job["error"]


def test_pipeline_reports_a_tailoring_mismatch(fake_db, stub_gemini, monkeypatch):
    def mismatched(resume, jd_text, settings):
        raise TailoringMismatchError("model returned 3 bullets for a 2-bullet entry")

    monkeypatch.setattr(app_main, "tailor_resume", mismatched)
    job = run_pipeline(fake_db)

    assert job["status"] == "error"
    assert "Tailoring failed" in job["error"]


def test_pipeline_reports_an_unexpected_model_error(fake_db, stub_gemini, monkeypatch):
    """Gemini auth/quota/network failures all land in the catch-all branch."""

    def boom(resume, jd_text, settings):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(app_main, "tailor_resume", boom)
    job = run_pipeline(fake_db)

    assert job["status"] == "error"
    assert "Generation failed" in job["error"]
    assert "RESOURCE_EXHAUSTED" in job["error"]


@pytest.mark.tectonic
def test_pipeline_attaches_the_log_when_latex_fails(fake_db, stub_gemini, monkeypatch):
    """A compile failure has to carry the Tectonic log, or the error is undiagnosable."""
    monkeypatch.setattr(
        app_main,
        "render_resume_latex",
        lambda resume, has_photo=False: (
            r"\documentclass{article}\begin{document}\undefinedcommand\end{document}"
        ),
    )
    job = run_pipeline(fake_db)

    assert job["status"] == "error"
    assert "Undefined control sequence" in job["error_detail"]


@pytest.mark.tectonic
def test_a_failed_cover_letter_does_not_sink_the_resume(fake_db, stub_gemini, monkeypatch):
    """Deliberate design: the resume is the primary artifact. A cover-letter failure must
    degrade to a warning on an otherwise complete job."""

    def boom(resume, jd_text, settings):
        raise RuntimeError("model refused")

    monkeypatch.setattr(app_main, "generate_cover_letter", boom)
    job = run_pipeline(fake_db)

    assert job["status"] == "done"
    assert job["percent"] == 100
    assert fake_db.documents[job["pdf_storage_path"]][:4] == b"%PDF"
    assert job["cover_letter_storage_path"] is None
    assert "model refused" in job["cover_letter_error"]
