from __future__ import annotations

import datetime
import uuid

import httpx
from supabase import Client, ClientOptions, create_client

from app.config import Settings

_settings = Settings()

# postgrest/storage3 build their httpx client with http2=True by default, and httpcore's
# *sync* HTTP/2 transport multiplexes every request over a single shared socket. This client
# is used from several threads at once -- the generation pipeline runs in a background thread
# while the browser polls /jobs/{id} through FastAPI's threadpool -- and that concurrent use
# of one socket surfaces on Windows as:
#   httpx.ReadError: [WinError 10035] A non-blocking socket operation could not be
#   completed immediately
# HTTP/1.1 has no multiplexing, so httpx's pool hands each thread its own connection and the
# contention goes away. Injecting the client is safe because postgrest and storage3 build
# absolute URLs and pass auth headers per request, so nothing here depends on the client's
# own base_url/headers.
_http_client = httpx.Client(http2=False, follow_redirects=True, timeout=30.0)

_client: Client = create_client(
    _settings.supabase_url,
    _settings.supabase_service_role_key,
    options=ClientOptions(httpx_client=_http_client),
)


def get_resume_markdown(user_id: str) -> str:
    resp = _client.table("resumes").select("markdown_text").eq("user_id", user_id).execute()
    if not resp.data:
        return ""
    return resp.data[0]["markdown_text"] or ""


def save_resume_markdown(user_id: str, markdown_text: str) -> None:
    _client.table("resumes").upsert(
        {"user_id": user_id, "markdown_text": markdown_text}
    ).execute()


def _get_photo_storage_path(user_id: str) -> str | None:
    resp = (
        _client.table("resumes").select("photo_storage_path").eq("user_id", user_id).execute()
    )
    if not resp.data:
        return None
    return resp.data[0]["photo_storage_path"]


def get_photo_bytes(user_id: str) -> bytes | None:
    path = _get_photo_storage_path(user_id)
    if not path:
        return None
    try:
        return _client.storage.from_(_settings.photo_bucket).download(path)
    except Exception:
        return None


def save_photo(user_id: str, content: bytes, content_type: str = "image/jpeg") -> None:
    # Supabase Storage sits behind a CDN that can keep serving a deleted/replaced
    # object from a reused path, so every upload gets a fresh, never-reused path —
    # the current path is tracked in resumes.photo_storage_path.
    old_path = _get_photo_storage_path(user_id)
    new_path = f"{user_id}/{uuid.uuid4().hex}.jpg"

    _client.storage.from_(_settings.photo_bucket).upload(
        new_path, content, {"content-type": content_type}
    )
    _client.table("resumes").upsert(
        {"user_id": user_id, "photo_storage_path": new_path}
    ).execute()

    if old_path:
        try:
            _client.storage.from_(_settings.photo_bucket).remove([old_path])
        except Exception:
            pass


def delete_photo(user_id: str) -> None:
    old_path = _get_photo_storage_path(user_id)
    _client.table("resumes").upsert(
        {"user_id": user_id, "photo_storage_path": None}
    ).execute()
    if old_path:
        try:
            _client.storage.from_(_settings.photo_bucket).remove([old_path])
        except Exception:
            pass


def upload_document(
    user_id: str, filename: str, content: bytes, content_type: str = "application/pdf"
) -> str:
    """Upload a generated artifact (resume PDF, cover letter .docx) to the documents bucket."""
    storage_path = f"{user_id}/{filename}"
    _client.storage.from_(_settings.pdf_bucket).upload(
        storage_path, content, {"content-type": content_type, "upsert": "true"}
    )
    return storage_path


def create_signed_document_url(storage_path: str, expires_in: int = 3600) -> str:
    result = _client.storage.from_(_settings.pdf_bucket).create_signed_url(
        storage_path, expires_in
    )
    return result.get("signedUrl") or result.get("signedURL")


def create_job(user_id: str) -> str:
    resp = (
        _client.table("generation_jobs")
        .insert({"user_id": user_id, "status": "running", "stage": "Starting...", "percent": 0})
        .execute()
    )
    return resp.data[0]["id"]


def update_job(job_id: str, **fields) -> None:
    _client.table("generation_jobs").update(fields).eq("id", job_id).execute()


def get_job(job_id: str, user_id: str) -> dict | None:
    resp = (
        _client.table("generation_jobs")
        .select("*")
        .eq("id", job_id)
        .eq("user_id", user_id)
        .execute()
    )
    if not resp.data:
        return None
    return resp.data[0]


def get_daily_usage(user_id: str) -> int:
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    resp = (
        _client.table("usage_counters")
        .select("generation_count")
        .eq("user_id", user_id)
        .eq("usage_date", today)
        .execute()
    )
    if not resp.data:
        return 0
    return resp.data[0]["generation_count"]


def increment_usage(user_id: str) -> int:
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    resp = _client.rpc("increment_usage", {"p_user_id": user_id, "p_date": today}).execute()
    return resp.data
