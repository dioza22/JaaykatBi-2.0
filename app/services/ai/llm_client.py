"""Thin wrapper around Gemini 2.5 Flash-Lite (free tier for MVP validation).

Kept as a single narrow class so swapping providers later (paid Gemini tier,
or Claude if quality/volume demands it) is a contained change — nothing
outside this module knows which LLM is behind `generate()`.

Passes proper multi-turn history via `contents=[Content(role=..., parts=...)]`
this time, rather than the old build's trick of flattening history into a
fake first turn.
"""

import logging

from google import genai
from google.genai import types

from app.config import get_settings

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.gemini_model
        self._client = genai.Client(api_key=settings.gemini_api_key) if settings.gemini_api_key else None

    async def generate(self, system_prompt: str, history: list[tuple[str, str]], user_message: str) -> str | None:
        """`history` is a list of (role, text) pairs, role in {"user", "model"},
        oldest first. Returns None on failure or if no API key is configured —
        callers must have a canned fallback response ready."""
        if self._client is None:
            return None

        contents = [types.Content(role=role, parts=[types.Part(text=text)]) for role, text in history]
        contents.append(types.Content(role="user", parts=[types.Part(text=user_message)]))

        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.7,
                    max_output_tokens=512,
                ),
            )
            return response.text
        except Exception:
            logger.exception("Gemini call failed")
            return None
