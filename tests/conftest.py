"""Hermetic environment for the test suite.

``app.auth``, ``app.db`` and ``app.main`` all instantiate ``Settings()`` at *module import*
time, and ``Settings`` has four required fields with no defaults -- so importing ``app.auth``
with a bare environment fails during collection, before a single test runs. pytest imports a
directory's conftest before it collects any test module in that directory, so setting the
variables here happens early enough.

These are *set*, not ``setdefault``'d: pydantic-settings ranks ``os.environ`` above the dotenv
file, so hard-setting them means a developer's real ``.env`` can never bleed into a test run.
``TECTONIC_BIN`` is intentionally left untouched -- local Windows development needs the absolute
path from ``.env``, while CI wants whatever is on ``PATH``.
"""

from __future__ import annotations

import io
import os

import pytest

_HERMETIC_ENV = {
    # supabase-py validates this against ^(https?)://.+ inside create_client(), which app/db.py
    # calls at import time -- so it has to look like a real https URL, not a placeholder word.
    "SUPABASE_URL": "https://ci-dummy.supabase.co",
    "SUPABASE_ANON_KEY": "ci-dummy-anon-key",
    "SUPABASE_SERVICE_ROLE_KEY": "ci-dummy-service-role-key",
    "GEMINI_API_KEY": "ci-dummy-gemini-key",
    "GEMINI_MODEL": "gemini-3.5-flash",
    "PHOTO_BUCKET": "photo",
    "PDF_BUCKET": "resume-pdf",
    "DAILY_GENERATION_LIMIT": "5",
}

for _key, _value in _HERMETIC_ENV.items():
    os.environ[_key] = _value


def pytest_configure(config: pytest.Config) -> None:
    """Fail loudly rather than silently talking to production.

    Cheap insurance: if the shadowing above ever stops working -- a pydantic-settings precedence
    change, someone passing an init kwarg, a stray SUPABASE_URL exported in a shell -- the suite
    would otherwise quietly point at the real Supabase project and the real Gemini key.
    """
    from app.config import Settings

    settings = Settings()
    if settings.supabase_url != _HERMETIC_ENV["SUPABASE_URL"]:
        raise pytest.UsageError(
            f"non-hermetic test environment: SUPABASE_URL resolved to "
            f"{settings.supabase_url!r}, expected the dummy value. A real .env or a shell "
            f"export is leaking into the test run; refusing to continue."
        )


@pytest.fixture(scope="session")
def photo_jpeg_bytes() -> bytes:
    r"""A neutral JPEG for tests that need an image resource.

    Generated with Pillow (already a runtime dependency) rather than committed, so the owner's
    real data/photo.jpg never has to live in a public repository -- data/ is gitignored.
    """
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (120, 160), (200, 200, 200)).save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()
