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

    daily_generation_limit: int = 5

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
