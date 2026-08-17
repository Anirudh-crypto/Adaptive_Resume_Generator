from __future__ import annotations

import hashlib
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db
from app.auth import get_current_user_id
from app.compile_service import LatexCompileError, compile_pdf
from app.config import Settings
from app.cover_letter import generate_cover_letter
from app.docx_render import DOCX_CONTENT_TYPE, render_cover_letter_docx
from app.latex_render import DEFAULT_REGION, LAYOUTS, PHOTO_RESOURCE_NAME, render_resume_latex
from app.pdf_import import (
    PdfImportError,
    extract_photo_from_pdf,
    extract_text_from_pdf,
    structure_resume_from_text,
)
from app.resume_parser import ResumeParseError, parse_resume_text, resume_to_markdown
from app.tailor import TailoringMismatchError, tailor_resume

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
settings = Settings()

STATIC_DIR = Path("app/static")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory="app/templates")


def static_url(filename: str) -> str:
    """Fingerprint static asset URLs with a hash of their contents.

    Templates are rendered per request and never cached server-side, but /static files are served
    by StaticFiles with an etag and no Cache-Control -- so browsers fall back to heuristic
    freshness and can serve a stale asset for a long time without revalidating. After a deploy
    that leaves a browser running the previous release's JS against the new release's HTML,
    which fails silently rather than loudly. A content hash in the query string makes a changed
    file a different URL, so the stale copy can never be reused.

    This only closes one direction. The HTML document had no Cache-Control either, so a browser
    could equally serve a stale *page* against the new release's assets -- the same mismatch, from
    the other side, and it looks like a feature simply not existing. `_html_response` handles that
    half; the two mechanisms are a pair and neither is sufficient alone.
    """
    try:
        digest = hashlib.sha256(STATIC_DIR.joinpath(filename).read_bytes()).hexdigest()
    except OSError:
        return f"/static/{filename}"
    return f"/static/{filename}?v={digest[:12]}"


templates.env.globals["static_url"] = static_url


class GenerateRequest(BaseModel):
    job_description: str
    # Picks the resume layout. A Literal gives a 422 on anything unexpected for free, and keeps
    # caller-supplied text from ever reaching the Jinja template loader.
    region: Literal["germany", "india"] = DEFAULT_REGION


class ResumeTextRequest(BaseModel):
    markdown_text: str


def _claim_slot_or_429(user_id: str) -> tuple[str | None, JSONResponse | None]:
    """Reserve one generation, or explain why we can't.

    Returns `(event_id, None)` on success -- and the caller **must** release that id if the work it
    paid for then fails, or the user is charged for a document they never received.
    Returns `(None, response)` when the user is at the limit.
    """
    event_id = db.claim_generation_slot(
        user_id, settings.generation_limit, settings.generation_window_hours
    )
    if event_id is not None:
        return event_id, None

    usage = db.get_usage(user_id, settings.generation_limit, settings.generation_window_hours)
    hours = settings.generation_window_hours
    message = (
        f"Limit of {settings.generation_limit} generations per {hours} "
        f"hour{'s' if hours != 1 else ''} reached."
    )
    if usage.get("next_reset_at"):
        message += " The next one frees up at " + usage["next_reset_at"] + "."
    return None, JSONResponse(status_code=429, content={"error": message})


def _supabase_context() -> dict:
    return {
        "supabase_url": settings.supabase_url,
        "supabase_anon_key": settings.supabase_anon_key,
    }


def _html_response(request: Request, template_name: str) -> Response:
    """Render a page, and tell the browser never to keep a copy of it.

    The counterpart to `static_url`. Assets are fingerprinted so a stale one can never be reused;
    without this the *document* was still cacheable, since neither Starlette nor Cloud Run sets any
    Cache-Control on it. A browser holding yesterday's HTML against today's JS fails the same silent
    way -- most visibly, a newly shipped control simply isn't in the markup, so the feature looks
    broken rather than stale.

    no-store rather than no-cache because TemplateResponse carries no ETag or Last-Modified, so
    there is nothing for a revalidation request to match against; it would refetch in full anyway.
    The pages are tiny and the fingerprinted assets they reference stay cacheable, so this costs
    almost nothing.
    """
    response = templates.TemplateResponse(request, template_name, _supabase_context())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index(request: Request):
    return _html_response(request, "index.html")


@app.get("/profile")
def profile(request: Request):
    return _html_response(request, "profile.html")


@app.get("/health")
def health():
    compiler_ok = shutil.which(settings.tectonic_bin) is not None
    return {"compiler_online": compiler_ok}


@app.get("/me/resume")
def get_my_resume(user_id: str = Depends(get_current_user_id)):
    return {"markdown_text": db.get_resume_markdown(user_id)}


@app.post("/me/resume")
def save_my_resume(req: ResumeTextRequest, user_id: str = Depends(get_current_user_id)):
    try:
        parse_resume_text(req.markdown_text)
    except ResumeParseError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    db.save_resume_markdown(user_id, req.markdown_text)
    return {"status": "saved"}


@app.post("/me/resume/import")
def import_my_resume(
    file: UploadFile = File(...), user_id: str = Depends(get_current_user_id)
):
    event_id, limited = _claim_slot_or_429(user_id)
    if limited:
        return limited

    file_bytes = file.file.read()

    # Every failure path below releases the slot: an import that produced nothing must not cost the
    # user a generation. A scanned PDF with no extractable text and an overloaded Gemini are both
    # things they'd want to simply retry.
    try:
        raw_text = extract_text_from_pdf(file_bytes)
        resume = structure_resume_from_text(raw_text, settings)
    except PdfImportError as exc:
        db.release_generation_slot(event_id)
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except Exception as exc:
        db.release_generation_slot(event_id)
        logger.exception("PDF import failed")
        return JSONResponse(status_code=502, content={"error": f"PDF import failed: {exc}"})

    photo_extracted = False
    try:
        photo_bytes = extract_photo_from_pdf(file_bytes)
        if photo_bytes is not None:
            db.save_photo(user_id, photo_bytes, content_type="image/jpeg")
            photo_extracted = True
    except Exception:
        logger.exception("Photo extraction from PDF failed (non-fatal, continuing)")

    return {"markdown_text": resume_to_markdown(resume), "photo_extracted": photo_extracted}


@app.get("/me/photo")
def get_my_photo(user_id: str = Depends(get_current_user_id)):
    content = db.get_photo_bytes(user_id)
    if content is None:
        raise HTTPException(status_code=404, detail="No photo uploaded")
    return Response(content=content, media_type="image/jpeg")


@app.post("/me/photo")
def upload_my_photo(file: UploadFile = File(...), user_id: str = Depends(get_current_user_id)):
    content = file.file.read()
    if len(content) > 5 * 1024 * 1024:
        return JSONResponse(status_code=400, content={"error": "Photo must be under 5MB"})
    if not (file.content_type or "").startswith("image/"):
        return JSONResponse(status_code=400, content={"error": "File must be an image"})
    db.save_photo(user_id, content, content_type=file.content_type or "image/jpeg")
    return {"status": "saved"}


@app.delete("/me/photo")
def delete_my_photo(user_id: str = Depends(get_current_user_id)):
    db.delete_photo(user_id)
    return {"status": "deleted"}


@app.get("/me/usage")
def get_my_usage(user_id: str = Depends(get_current_user_id)):
    return db.get_usage(user_id, settings.generation_limit, settings.generation_window_hours)


@app.post("/generate", status_code=202)
def generate(req: GenerateRequest, user_id: str = Depends(get_current_user_id)):
    if not req.job_description.strip():
        return JSONResponse(status_code=400, content={"error": "Job description is empty"})

    event_id, limited = _claim_slot_or_429(user_id)
    if limited:
        return limited

    job_id = db.create_job(user_id)

    threading.Thread(
        target=_run_pipeline,
        args=(job_id, user_id, req.job_description, req.region, event_id),
        daemon=True,
    ).start()

    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str, user_id: str = Depends(get_current_user_id)):
    job = db.get_job(job_id, user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    if job["status"] == "done":
        if job.get("pdf_storage_path"):
            job["pdf_url"] = db.create_signed_document_url(job["pdf_storage_path"])
        if job.get("cover_letter_storage_path"):
            job["cover_letter_url"] = db.create_signed_document_url(
                job["cover_letter_storage_path"]
            )
    return job


def _run_pipeline(
    job_id: str,
    user_id: str,
    job_description: str,
    region: str = DEFAULT_REGION,
    event_id: str | None = None,
) -> None:
    """Run the generation pipeline. Runs on a background thread; /generate has already returned 202.

    `event_id` is the rate-limit slot the request reserved. Every path that ends with the job in
    `status="error"` releases it -- the user produced no document, so it must not cost them a
    generation. A failed cover letter deliberately does *not* release: the job still completes and
    the resume PDF ships, which is the artifact they asked for.
    """
    try:
        db.update_job(job_id, stage="Parsing resume...", percent=5)
        markdown_text = db.get_resume_markdown(user_id)
        if not markdown_text.strip():
            raise ResumeParseError("No resume saved yet — add one on the profile page first.")
        resume = parse_resume_text(markdown_text)

        db.update_job(job_id, stage="Tailoring resume with Gemini...", percent=15)
        tailored = tailor_resume(resume, job_description, settings)

        db.update_job(job_id, stage="Writing cover letter...", percent=40)
        cover_letter_docx = None
        cover_letter_error = None
        try:
            letter = generate_cover_letter(tailored, job_description, settings)
            cover_letter_docx = render_cover_letter_docx(letter, tailored)
        except Exception as exc:
            # The resume is the primary artifact — a failed cover letter shouldn't sink the job.
            logger.exception("Cover letter generation failed (non-fatal, continuing)")
            cover_letter_error = f"Cover letter could not be generated: {exc}"

        db.update_job(job_id, stage="Rendering LaTeX...", percent=60)
        # Whether a photo is used is the layout's decision, not the region's -- the Indian layout has
        # nowhere to put one, so a user with a photo saved doesn't get a stray pic.JPG written into
        # the compile directory and referenced by nothing.
        layout = LAYOUTS[region]
        photo_bytes = db.get_photo_bytes(user_id) if layout.supports_photo else None
        has_photo = photo_bytes is not None
        tex_source = render_resume_latex(tailored, has_photo=has_photo, region=region)

        db.update_job(job_id, stage="Compiling PDF...", percent=70)
        resources = {PHOTO_RESOURCE_NAME: photo_bytes} if has_photo else {}
        pdf_bytes = compile_pdf(tex_source, settings, resources=resources)

        db.update_job(job_id, stage="Saving documents...", percent=90)
        stamp = int(time.time())
        storage_path = db.upload_document(user_id, f"resume_{stamp}.pdf", pdf_bytes)

        cover_letter_path = None
        if cover_letter_docx is not None:
            cover_letter_path = db.upload_document(
                user_id,
                f"cover_letter_{stamp}.docx",
                cover_letter_docx,
                content_type=DOCX_CONTENT_TYPE,
            )

        db.update_job(
            job_id,
            status="done",
            stage="Done",
            percent=100,
            pdf_storage_path=storage_path,
            cover_letter_storage_path=cover_letter_path,
            cover_letter_error=cover_letter_error,
        )
    except ResumeParseError as exc:
        logger.exception("Resume parse error")
        db.update_job(job_id, status="error", error=f"Could not parse resume: {exc}")
        _release_slot(event_id)
    except TailoringMismatchError as exc:
        logger.exception("Tailoring mismatch")
        db.update_job(job_id, status="error", error=f"Tailoring failed: {exc}")
        _release_slot(event_id)
    except LatexCompileError as exc:
        logger.exception("LaTeX compile error")
        db.update_job(job_id, status="error", error=str(exc), error_detail=exc.log)
        _release_slot(event_id)
    except Exception as exc:  # Gemini API errors (auth, quota, network, ...)
        logger.exception("Pipeline failed")
        db.update_job(job_id, status="error", error=f"Generation failed: {exc}")
        _release_slot(event_id)


def _release_slot(event_id: str | None) -> None:
    """Hand a rate-limit slot back after a failed generation.

    Never allowed to mask the original failure: this runs inside an exception handler that has
    already recorded why the job died, and a refund that itself blows up would replace a useful
    error message with a confusing one.
    """
    if event_id is None:
        return
    try:
        db.release_generation_slot(event_id)
    except Exception:
        logger.exception("Could not release rate-limit slot %s", event_id)
