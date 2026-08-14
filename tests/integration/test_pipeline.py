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


def run_pipeline(
    fake_db,
    *,
    resume_markdown: str | None = SAMPLE_RESUME_MD,
    region: str = "germany",
    claim_slot: bool = True,
) -> dict:
    """Create a job, run the pipeline to completion, and return the final job row.

    Claims a real rate-limit slot first, mirroring what POST /generate does, so the tests can assert
    whether a given outcome gives that slot back.
    """
    if resume_markdown is not None:
        fake_db.resumes[TEST_USER] = resume_markdown
    event_id = fake_db.claim_generation_slot(TEST_USER, 5, 5) if claim_slot else None
    job_id = fake_db.create_job(TEST_USER)
    app_main._run_pipeline(job_id, TEST_USER, JOB_DESCRIPTION, region, event_id)
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
def test_indian_layout_compiles_without_the_photo(fake_db, stub_gemini, jpeg_bytes):
    """Even with a photo saved, the Indian layout must not receive it: that layout has no
    \\includegraphics, so shipping the bytes would put an unreferenced file in the compile dir."""
    fake_db.photos[TEST_USER] = jpeg_bytes
    job = run_pipeline(fake_db, region="india")

    assert job["status"] == "done"
    assert fake_db.documents[job["pdf_storage_path"]][:4] == b"%PDF"


def test_photo_resources_follow_the_layout(fake_db, stub_gemini, jpeg_bytes, monkeypatch):
    """Asserts the resource dict directly, so this stays meaningful without a LaTeX toolchain."""
    seen: dict[str, dict] = {}

    def capture(tex_source, settings, resources=None):
        seen["resources"] = resources or {}
        return b"%PDF-fake"

    monkeypatch.setattr(app_main, "compile_pdf", capture)
    fake_db.photos[TEST_USER] = jpeg_bytes

    run_pipeline(fake_db, region="india")
    assert seen["resources"] == {}

    run_pipeline(fake_db, region="germany")
    assert list(seen["resources"]) == ["pic.JPG"]


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
        lambda resume, has_photo=False, region="germany": (
            r"\documentclass{article}\begin{document}\undefinedcommand\end{document}"
        ),
    )
    job = run_pipeline(fake_db)

    assert job["status"] == "error"
    assert "Undefined control sequence" in job["error_detail"]


# --- rate-limit slot accounting --------------------------------------------------------------
#
# The regression guard for the reported bug: a Gemini 503 used to leave the generation charged even
# though the user got nothing back.


@pytest.mark.tectonic
def test_a_successful_generation_keeps_its_slot(fake_db, stub_gemini):
    run_pipeline(fake_db)
    assert fake_db.slots_used(TEST_USER) == 1


@pytest.mark.parametrize(
    ("name", "setup"),
    [
        # No resume saved -- fails before any model call is even made.
        ("no resume", lambda mp: None),
        (
            "tailoring mismatch",
            lambda mp: mp.setattr(
                app_main,
                "tailor_resume",
                lambda r, j, s: (_ for _ in ()).throw(TailoringMismatchError("bad shape")),
            ),
        ),
        (
            "model overloaded",
            lambda mp: mp.setattr(
                app_main,
                "tailor_resume",
                lambda r, j, s: (_ for _ in ()).throw(RuntimeError("503 UNAVAILABLE")),
            ),
        ),
    ],
)
def test_a_failed_generation_gives_its_slot_back(fake_db, stub_gemini, monkeypatch, name, setup):
    setup(monkeypatch)
    resume = None if name == "no resume" else SAMPLE_RESUME_MD
    job = run_pipeline(fake_db, resume_markdown=resume if resume else "")

    assert job["status"] == "error", name
    assert fake_db.slots_used(TEST_USER) == 0, f"{name} should not cost a generation"


@pytest.mark.tectonic
def test_a_latex_failure_gives_its_slot_back(fake_db, stub_gemini, monkeypatch):
    monkeypatch.setattr(
        app_main,
        "render_resume_latex",
        lambda resume, has_photo=False, region="germany": (
            r"\documentclass{article}\begin{document}\undefinedcommand\end{document}"
        ),
    )
    job = run_pipeline(fake_db)

    assert job["status"] == "error"
    assert fake_db.slots_used(TEST_USER) == 0


def test_a_release_failure_does_not_mask_the_original_error(fake_db, stub_gemini, monkeypatch):
    """The refund runs inside an exception handler that has already recorded why the job died.
    If the refund itself explodes, that diagnosis must survive."""
    monkeypatch.setattr(
        app_main,
        "tailor_resume",
        lambda r, j, s: (_ for _ in ()).throw(RuntimeError("503 UNAVAILABLE")),
    )

    def boom(event_id):
        raise RuntimeError("supabase unreachable")

    monkeypatch.setattr(app_db, "release_generation_slot", boom)

    job = run_pipeline(fake_db)
    assert job["status"] == "error"
    assert "503 UNAVAILABLE" in job["error"]


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
    # Deliberately still charged: the resume PDF -- the thing they asked for -- was delivered.
    assert fake_db.slots_used(TEST_USER) == 1
