from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    gemini_api_key: str
    gemini_model: str = "gemini-3.5-flash"
    tectonic_bin: str = "tectonic"

    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str
    photo_bucket: str = "photo"
    # Holds every generated artifact — tailored resume PDFs and cover letter .docx files.
    pdf_bucket: str = "resume-pdf"

    # Sliding-window rate limit on the shared Gemini key: at most `generation_limit` generations in
    # any rolling `generation_window_hours`. Sliding rather than per-calendar-day so there is no
    # burst at a bucket boundary.
    generation_limit: int = 5
    generation_window_hours: int = 5

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
