"""Route-level integration tests: the real app, exercised over HTTP by TestClient."""

from __future__ import annotations

import re

import pytest

from app import main as app_main

from .conftest import SAMPLE_RESUME_MD, TEST_USER

pytestmark = pytest.mark.integration


# --- pages and static assets ---------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/profile"])
def test_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


@pytest.mark.parametrize("path", ["/", "/profile"])
def test_pages_reference_fingerprinted_assets_that_resolve(client, path):
    """Guards the stale-asset failure mode static_url() exists to prevent.

    Static files are served with an etag but no Cache-Control, so a browser can reuse a cached
    copy for a long time without revalidating. After a deploy that leaves the previous release's
    JS running against the new release's markup -- which fails silently. The content hash makes a
    changed file a different URL. Two things have to hold: the template must actually call
    static_url (not hardcode /static/...), and the URL it produces must be fetchable.
    """
    html = client.get(path).text
    fingerprinted = re.findall(r'/static/([\w./-]+)\?v=([0-9a-f]{12})\b', html)
    assert fingerprinted, f"{path} references no fingerprinted assets: static_url() not applied"

    for filename, digest in fingerprinted:
        asset = client.get(f"/static/{filename}?v={digest}")
        assert asset.status_code == 200, f"/static/{filename} did not resolve"


def test_static_url_digest_tracks_file_contents(tmp_path, monkeypatch):
    """The fingerprint must change when the file changes, or it isn't doing anything."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    asset = static_dir / "app.js"
    monkeypatch.setattr(app_main, "STATIC_DIR", static_dir)

    asset.write_text("console.log('v1');", encoding="utf-8")
    first = app_main.static_url("app.js")

    asset.write_text("console.log('v2');", encoding="utf-8")
    second = app_main.static_url("app.js")

    assert first != second
    assert "?v=" in first and "?v=" in second


def test_static_url_falls_back_when_file_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(app_main, "STATIC_DIR", tmp_path)
    assert app_main.static_url("nope.js") == "/static/nope.js"


def test_index_passes_supabase_config_to_the_browser(client):
    """The anon key and URL are deliberately handed to the client-side Supabase SDK."""
    html = client.get("/").text
    assert "https://ci-dummy.supabase.co" in html
    assert "ci-dummy-anon-key" in html


def test_health_reports_compiler_state(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert isinstance(response.json()["compiler_online"], bool)


# --- authentication ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/me/resume"),
        ("POST", "/me/resume"),
        ("POST", "/me/resume/import"),
        ("GET", "/me/photo"),
        ("POST", "/me/photo"),
        ("DELETE", "/me/photo"),
        ("POST", "/generate"),
        ("GET", "/jobs/some-job-id"),
    ],
)
def test_protected_routes_require_a_token(anon_client, method, path):
    response = anon_client.request(method, path)
    assert response.status_code == 401


@pytest.mark.parametrize("header", ["Bearer this.is.garbage", "Bearer ", "Token abc", ""])
def test_malformed_authorization_headers_are_rejected(anon_client, header):
    """Stays offline: a malformed token fails while decoding its header, before PyJWKClient
    would ever fetch the JWKS document."""
    response = anon_client.get("/me/resume", headers={"Authorization": header})
    assert response.status_code == 401


# --- resume --------------------------------------------------------------------------------


def test_resume_round_trips(client, fake_db):
    assert client.get("/me/resume").json() == {"markdown_text": ""}

    saved = client.post("/me/resume", json={"markdown_text": SAMPLE_RESUME_MD})
    assert saved.status_code == 200
    assert saved.json() == {"status": "saved"}
    assert fake_db.resumes[TEST_USER] == SAMPLE_RESUME_MD

    assert client.get("/me/resume").json() == {"markdown_text": SAMPLE_RESUME_MD}


@pytest.mark.parametrize(
    ("markdown", "reason"),
    [
        ("This does not start with a heading.", "no '# Name' heading"),
        ("# Jane Doe\n\nEmail: jane@example.com\n\n## Experience\n\nstuff\n", "no '## Summary'"),
        ("", "empty document"),
    ],
)
def test_saving_an_unparseable_resume_is_rejected(client, fake_db, markdown, reason):
    response = client.post("/me/resume", json={"markdown_text": markdown})
    assert response.status_code == 400, reason
    assert "error" in response.json()
    # A rejected document must not overwrite whatever was stored before.
    assert TEST_USER not in fake_db.resumes


def test_saving_a_resume_requires_the_field(client):
    assert client.post("/me/resume", json={}).status_code == 422


# --- photo ---------------------------------------------------------------------------------


def test_photo_upload_get_delete_cycle(client, fake_db, jpeg_bytes):
    assert client.get("/me/photo").status_code == 404

    upload = client.post("/me/photo", files={"file": ("me.jpg", jpeg_bytes, "image/jpeg")})
    assert upload.status_code == 200
    assert fake_db.photos[TEST_USER] == jpeg_bytes

    fetched = client.get("/me/photo")
    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "image/jpeg"
    assert fetched.content == jpeg_bytes

    assert client.delete("/me/photo").status_code == 200
    assert client.get("/me/photo").status_code == 404


def test_photo_over_5mb_is_rejected(client, fake_db):
    oversized = b"\xff\xd8\xff" + b"0" * (5 * 1024 * 1024)
    response = client.post("/me/photo", files={"file": ("big.jpg", oversized, "image/jpeg")})
    assert response.status_code == 400
    assert "5MB" in response.json()["error"]
    assert TEST_USER not in fake_db.photos


def test_non_image_upload_is_rejected(client, fake_db):
    response = client.post("/me/photo", files={"file": ("cv.pdf", b"%PDF-1.4", "application/pdf")})
    assert response.status_code == 400
    assert "image" in response.json()["error"].lower()
    assert TEST_USER not in fake_db.photos


# --- PDF import ----------------------------------------------------------------------------


def test_import_returns_markdown(client, fake_db, monkeypatch, stub_gemini):
    # extract_text_from_pdf is real pypdf; give it text to find rather than building a
    # text-bearing PDF, which would need a LaTeX toolchain just to set up.
    monkeypatch.setattr(
        app_main, "extract_text_from_pdf", lambda file_bytes: "Jane Doe\nBackend engineer"
    )
    response = client.post(
        "/me/resume/import", files={"file": ("cv.pdf", b"%PDF-1.4 fake", "application/pdf")}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["markdown_text"].startswith("# Jane Doe")
    assert body["photo_extracted"] is False
    assert stub_gemini["structure_resume_from_text"] == "Jane Doe\nBackend engineer"


def test_import_rejects_a_pdf_with_no_text(client, blank_pdf_bytes):
    """The real pypdf path: a blank (or scanned) PDF yields no extractable text."""
    response = client.post(
        "/me/resume/import", files={"file": ("scan.pdf", blank_pdf_bytes, "application/pdf")}
    )
    assert response.status_code == 400
    assert "no extractable text" in response.json()["error"].lower()


def test_import_surfaces_model_failures_as_502(client, monkeypatch):
    monkeypatch.setattr(app_main, "extract_text_from_pdf", lambda file_bytes: "some text")

    def boom(raw_text, settings):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(app_main, "structure_resume_from_text", boom)
    response = client.post(
        "/me/resume/import", files={"file": ("cv.pdf", b"%PDF-1.4", "application/pdf")}
    )
    assert response.status_code == 502


def test_successful_import_consumes_a_slot(client, fake_db, monkeypatch, stub_gemini):
    monkeypatch.setattr(app_main, "extract_text_from_pdf", lambda file_bytes: "Jane Doe")
    client.post("/me/resume/import", files={"file": ("cv.pdf", b"%PDF-1.4", "application/pdf")})
    assert fake_db.slots_used(TEST_USER) == 1


def test_import_that_finds_no_text_costs_nothing(client, fake_db, blank_pdf_bytes):
    """The old behaviour charged before even reading the file, so an unreadable PDF cost a
    generation. Extracting nothing is exactly the case a user would retry."""
    response = client.post(
        "/me/resume/import", files={"file": ("scan.pdf", blank_pdf_bytes, "application/pdf")}
    )
    assert response.status_code == 400
    assert fake_db.slots_used(TEST_USER) == 0


def test_import_that_hits_a_model_error_costs_nothing(client, fake_db, monkeypatch):
    monkeypatch.setattr(app_main, "extract_text_from_pdf", lambda file_bytes: "some text")

    def boom(raw_text, settings):
        raise RuntimeError("503 UNAVAILABLE. The model is overloaded.")

    monkeypatch.setattr(app_main, "structure_resume_from_text", boom)
    response = client.post(
        "/me/resume/import", files={"file": ("cv.pdf", b"%PDF-1.4", "application/pdf")}
    )
    assert response.status_code == 502
    assert fake_db.slots_used(TEST_USER) == 0


# --- rate limiting -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "kwargs"),
    [
        ("/generate", {"json": {"job_description": "Senior Python role"}}),
        ("/me/resume/import", {"files": {"file": ("cv.pdf", b"%PDF-1.4", "application/pdf")}}),
    ],
)
def test_exhausted_window_returns_429(client, fake_db, path, kwargs):
    fake_db.fill_slots(TEST_USER, 5)  # GENERATION_LIMIT in tests/conftest.py
    response = client.post(path, **kwargs)
    assert response.status_code == 429
    error = response.json()["error"]
    assert "5 generations per 5 hours" in error
    assert "frees up at" in error


def test_generation_below_the_limit_is_allowed(client, fake_db, monkeypatch):
    monkeypatch.setattr(app_main, "_run_pipeline", lambda *args: None)
    fake_db.fill_slots(TEST_USER, 4)
    assert client.post("/generate", json={"job_description": "Backend role"}).status_code == 202


def test_slots_older_than_the_window_do_not_count(client, fake_db, monkeypatch):
    """The point of a sliding window: capacity returns as individual slots age out."""
    monkeypatch.setattr(app_main, "_run_pipeline", lambda *args: None)
    fake_db.fill_slots(TEST_USER, 5, age_hours=6)  # all beyond the 5-hour window
    assert client.post("/generate", json={"job_description": "Backend role"}).status_code == 202


def test_a_released_slot_frees_capacity_immediately(client, fake_db, monkeypatch):
    monkeypatch.setattr(app_main, "_run_pipeline", lambda *args: None)
    slot_ids = fake_db.fill_slots(TEST_USER, 5)
    assert client.post("/generate", json={"job_description": "Backend role"}).status_code == 429

    fake_db.release_generation_slot(slot_ids[0])
    assert client.post("/generate", json={"job_description": "Backend role"}).status_code == 202


def test_releasing_the_same_slot_twice_does_not_grant_extra_capacity(client, fake_db, monkeypatch):
    """Guards the idempotency the DELETE-based refund relies on."""
    monkeypatch.setattr(app_main, "_run_pipeline", lambda *args: None)
    slot_ids = fake_db.fill_slots(TEST_USER, 5)

    fake_db.release_generation_slot(slot_ids[0])
    fake_db.release_generation_slot(slot_ids[0])

    assert fake_db.slots_used(TEST_USER) == 4


# --- usage endpoint ------------------------------------------------------------------------


def test_usage_reports_remaining_slots(client, fake_db):
    assert client.get("/me/usage").json() == {
        "used": 0,
        "limit": 5,
        "remaining": 5,
        "next_reset_at": None,
    }

    fake_db.fill_slots(TEST_USER, 2)
    body = client.get("/me/usage").json()
    assert body["used"] == 2
    assert body["remaining"] == 3
    assert body["next_reset_at"] is not None


def test_usage_never_reports_negative_remaining(client, fake_db):
    """A hand-edited row or a race could push usage past the limit; the UI must not show "-1"."""
    fake_db.fill_slots(TEST_USER, 7)
    body = client.get("/me/usage").json()
    assert body["used"] == 7
    assert body["remaining"] == 0


def test_usage_ignores_other_users(client, fake_db):
    from .conftest import OTHER_USER

    fake_db.fill_slots(OTHER_USER, 5)
    assert client.get("/me/usage").json()["remaining"] == 5


# --- generate and job status ---------------------------------------------------------------


def test_generate_creates_a_job_and_charges_usage(client, fake_db, monkeypatch):
    # The route hands the pipeline to a background thread. Stub the pipeline out so this test
    # covers the route's own contract without racing a thread or compiling a real PDF --
    # _run_pipeline gets its own synchronous coverage in test_pipeline.py.
    started: list[tuple] = []
    monkeypatch.setattr(app_main, "_run_pipeline", lambda *args: started.append(args))

    fake_db.resumes[TEST_USER] = SAMPLE_RESUME_MD
    response = client.post("/generate", json={"job_description": "Senior Python role"})

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    # Both of these happen synchronously inside the route, before the thread is started, so
    # asserting them cannot race the background work.
    assert job_id in fake_db.jobs
    assert fake_db.jobs[job_id]["user_id"] == TEST_USER
    assert fake_db.slots_used(TEST_USER) == 1
    # The claimed slot is handed to the pipeline so it can be released if the job fails.
    assert started[0][4] is not None


@pytest.mark.parametrize("description", ["", "   ", "\n\t"])
def test_generate_rejects_an_empty_job_description(client, fake_db, description):
    response = client.post("/generate", json={"job_description": description})
    assert response.status_code == 400
    assert response.json()["error"] == "Job description is empty"
    # Rejected before any quota is spent.
    assert fake_db.generation_events == []


# --- region selection ----------------------------------------------------------------------


def test_generate_defaults_to_the_german_layout(client, fake_db, monkeypatch):
    started: list[tuple] = []
    monkeypatch.setattr(app_main, "_run_pipeline", lambda *args: started.append(args))

    client.post("/generate", json={"job_description": "Backend role"})
    assert started[0][3] == "germany"


def test_generate_passes_the_requested_region(client, fake_db, monkeypatch):
    started: list[tuple] = []
    monkeypatch.setattr(app_main, "_run_pipeline", lambda *args: started.append(args))

    client.post("/generate", json={"job_description": "Backend role", "region": "india"})
    assert started[0][3] == "india"


@pytest.mark.parametrize("region", ["france", "", "resume.tex.jinja", "../etc/passwd"])
def test_generate_rejects_an_unknown_region(client, fake_db, region):
    """The Literal keeps caller-controlled text away from the Jinja template loader."""
    response = client.post(
        "/generate", json={"job_description": "Backend role", "region": region}
    )
    assert response.status_code == 422
    assert fake_db.generation_events == []


def test_job_status_404s_for_an_unknown_id(client):
    assert client.get("/jobs/does-not-exist").status_code == 404


def test_job_status_404s_for_another_users_job(client, fake_db):
    """get_job scopes by user_id -- that scoping *is* the authorisation check."""
    from .conftest import OTHER_USER

    someone_elses = fake_db.create_job(OTHER_USER)
    assert client.get(f"/jobs/{someone_elses}").status_code == 404


def test_job_status_reports_progress_while_running(client, fake_db):
    job_id = fake_db.create_job(TEST_USER)
    fake_db.update_job(job_id, stage="Compiling PDF...", percent=70)

    body = client.get(f"/jobs/{job_id}").json()
    assert body["status"] == "running"
    assert body["percent"] == 70
    assert "pdf_url" not in body


def test_completed_job_exposes_signed_document_urls(client, fake_db):
    job_id = fake_db.create_job(TEST_USER)
    fake_db.update_job(
        job_id,
        status="done",
        percent=100,
        pdf_storage_path=f"{TEST_USER}/resume_1.pdf",
        cover_letter_storage_path=f"{TEST_USER}/cover_letter_1.docx",
    )

    body = client.get(f"/jobs/{job_id}").json()
    assert body["pdf_url"].startswith("https://storage.test/")
    assert body["cover_letter_url"].endswith("?token=signed&expires=3600")


def test_completed_job_without_a_cover_letter_omits_its_url(client, fake_db):
    job_id = fake_db.create_job(TEST_USER)
    fake_db.update_job(
        job_id, status="done", percent=100, pdf_storage_path=f"{TEST_USER}/resume_1.pdf"
    )

    body = client.get(f"/jobs/{job_id}").json()
    assert "pdf_url" in body
    assert "cover_letter_url" not in body
