from __future__ import annotations

from google import genai
from google.genai import types

from app.config import Settings


def build_client(settings: Settings) -> genai.Client:
    """A Gemini client with the SDK's retry policy switched on.

    google-genai defaults ``HttpOptions.retry_options`` to ``None``, which its ``retry_args`` helper
    turns into ``tenacity.stop_after_attempt(1)`` -- one attempt, no retries. Gemini returns
    503 "the model is overloaded" routinely, so without this a single transient blip fails an entire
    generation permanently, and the user is left to retry by hand.

    Passing the default ``HttpRetryOptions()`` opts into the backoff the SDK already ships: 5
    attempts over 1/2/4/8s, for 408/429/500/502/503/504 plus connect and timeout errors. Worst case
    adds ~15s, which is invisible on /generate (it runs on a background thread and the browser polls
    /jobs/{id}) and tolerable on the synchronous /me/resume/import.

    Every Gemini call in the app goes through here so the policy is defined once rather than
    repeated at each call site.
    """
    return genai.Client(
        api_key=settings.gemini_api_key,
        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions()),
    )
