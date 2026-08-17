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


def claim_generation_slot(user_id: str, limit: int, window_hours: int) -> str | None:
    """Reserve one generation against the sliding-window limit.

    Returns the new event's id, which the caller must hand to `release_generation_slot` if the work
    it paid for ends up failing. Returns None when the user is already at the limit.

    The count and the insert happen inside one `security definer` function holding a per-user
    advisory lock, so this is atomic -- checking and charging separately would let two concurrent
    requests both see room and both take it.
    """
    resp = _client.rpc(
        "claim_generation_slot",
        {"p_user_id": user_id, "p_limit": limit, "p_window": f"{window_hours} hours"},
    ).execute()
    return resp.data or None


def release_generation_slot(event_id: str) -> None:
    """Give a claimed slot back, because the work it paid for failed.

    Deleting the row is deliberately idempotent: releasing twice is a no-op rather than something
    that could hand out a free generation.
    """
    _client.table("generation_events").delete().eq("id", event_id).execute()


def get_usage(user_id: str, limit: int, window_hours: int) -> dict:
    """Current usage for the badge: how many slots are in use and when the next one frees up.

    A sliding window has no single reset instant -- capacity returns when the oldest event in the
    window ages out -- so `next_reset_at` is that event's timestamp plus the window, or None when
    the user has nothing in flight.
    """
    window_start = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=window_hours)
    resp = (
        _client.table("generation_events")
        .select("created_at")
        .eq("user_id", user_id)
        .gt("created_at", window_start.isoformat())
        .order("created_at")
        .execute()
    )
    events = resp.data or []

    next_reset_at = None
    if events:
        oldest = datetime.datetime.fromisoformat(events[0]["created_at"])
        next_reset_at = (oldest + datetime.timedelta(hours=window_hours)).isoformat()

    used = len(events)
    return {
        "used": used,
        "limit": limit,
        # Clamped because the advisory lock protects a single Postgres instance, not a hand-edited
        # row: a negative "remaining" would be alarming nonsense in the UI.
        "remaining": max(limit - used, 0),
        "next_reset_at": next_reset_at,
    }
