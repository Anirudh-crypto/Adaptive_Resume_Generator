from __future__ import annotations

import hashlib
import logging
import shutil
import threading
import time
from pathlib import Path

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
from app.latex_render import PHOTO_RESOURCE_NAME, render_resume_latex
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

    Templates are rendered per request and never cached, but /static files are served by
    StaticFiles with an etag and no Cache-Control -- so browsers fall back to heuristic
    freshness and can serve a stale asset for a long time without revalidating. After a deploy
    that leaves a browser running the previous release's JS against the new release's HTML,
    which fails silently rather than loudly. A content hash in the query string makes a changed
    file a different URL, so the stale copy can never be reused.
    """
    try:
        digest = hashlib.sha256(STATIC_DIR.joinpath(filename).read_bytes()).hexdigest()
    except OSError:
        return f"/static/{filename}"
    return f"/static/{filename}?v={digest[:12]}"


templates.env.globals["static_url"] = static_url


class GenerateRequest(BaseModel):
    job_description: str


class ResumeTextRequest(BaseModel):
    markdown_text: str


def _rate_limit_or_none(user_id: str) -> JSONResponse | None:
    if db.get_daily_usage(user_id) >= settings.daily_generation_limit:
        return JSONResponse(
            status_code=429,
            content={
                "error": (
                    f"Daily limit of {settings.daily_generation_limit} generations reached. "
                    "Try again tomorrow (resets 00:00 UTC)."
                )
            },
        )
    return None


def _supabase_context() -> dict:
    return {
        "supabase_url": settings.supabase_url,
        "supabase_anon_key": settings.supabase_anon_key,
    }


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _supabase_context())


@app.get("/profile")
def profile(request: Request):
    return templates.TemplateResponse(request, "profile.html", _supabase_context())


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
    limited = _rate_limit_or_none(user_id)
    if limited:
        return limited
    db.increment_usage(user_id)

    file_bytes = file.file.read()

    try:
        raw_text = extract_text_from_pdf(file_bytes)
        resume = structure_resume_from_text(raw_text, settings)
    except PdfImportError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except Exception as exc:
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


@app.post("/generate", status_code=202)
def generate(req: GenerateRequest, user_id: str = Depends(get_current_user_id)):
    if not req.job_description.strip():
        return JSONResponse(status_code=400, content={"error": "Job description is empty"})

    limited = _rate_limit_or_none(user_id)
    if limited:
        return limited

    db.increment_usage(user_id)
    job_id = db.create_job(user_id)

    threading.Thread(
        target=_run_pipeline, args=(job_id, user_id, req.job_description), daemon=True
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


def _run_pipeline(job_id: str, user_id: str, job_description: str) -> None:
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
        photo_bytes = db.get_photo_bytes(user_id)
        has_photo = photo_bytes is not None
        tex_source = render_resume_latex(tailored, has_photo=has_photo)

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
    except TailoringMismatchError as exc:
        logger.exception("Tailoring mismatch")
        db.update_job(job_id, status="error", error=f"Tailoring failed: {exc}")
    except LatexCompileError as exc:
        logger.exception("LaTeX compile error")
        db.update_job(job_id, status="error", error=str(exc), error_detail=exc.log)
    except Exception as exc:  # Gemini API errors (auth, quota, network, ...)
        logger.exception("Pipeline failed")
        db.update_job(job_id, status="error", error=f"Generation failed: {exc}")
