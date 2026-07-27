"""Outbound WhatsApp Cloud API calls. Limits/truncation below mirror the
WhatsApp API's own constraints (and the old C# WhatsAppService, which already
got these right)."""

import asyncio
import logging
import re

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()

# Meta's sandbox test numbers hit short-lived rate limits under rapid testing
# (observed as 400/403, not just the more obvious 429) — a couple of retries
# with backoff is enough for those to clear. Without this, a rate-limited send
# was just dropped silently and the merchant never got a reply.
_RETRYABLE_STATUS_CODES = {400, 403, 429, 500, 502, 503, 504}
_RETRY_DELAYS_SECONDS = [1, 3]  # 3 attempts total


def normalize_phone_number(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 9:  # bare local number -> assume Senegal
        digits = "221" + digits
    return digits


class WhatsAppClient:
    def __init__(self) -> None:
        self._base_url = f"https://graph.facebook.com/{_settings.whatsapp_api_version}"
        self._phone_number_id = _settings.whatsapp_phone_number_id
        self._headers = {
            "Authorization": f"Bearer {_settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        }

    async def _post(self, payload: dict) -> dict:
        url = f"{self._base_url}/{self._phone_number_id}/messages"
        attempts = len(_RETRY_DELAYS_SECONDS) + 1
        for attempt in range(attempts):
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(url, headers=self._headers, json=payload)
            if response.status_code < 400:
                return response.json()

            is_last_attempt = attempt == attempts - 1
            if response.status_code not in _RETRYABLE_STATUS_CODES or is_last_attempt:
                logger.warning(
                    "WhatsApp API call failed permanently after %d attempt(s): %s %s",
                    attempt + 1, response.status_code, response.text,
                )
                response.raise_for_status()

            logger.warning(
                "WhatsApp API call failed (attempt %d/%d): %s %s — retrying in %ds",
                attempt + 1, attempts, response.status_code, response.text, _RETRY_DELAYS_SECONDS[attempt],
            )
            await asyncio.sleep(_RETRY_DELAYS_SECONDS[attempt])

        raise AssertionError("unreachable")  # loop always returns or raises above

    async def send_text(self, to: str, body: str) -> dict:
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "to": normalize_phone_number(to),
                "type": "text",
                "text": {"body": body, "preview_url": False},
            }
        )

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> dict:
        """buttons: list of (id, title). Max 3, titles truncated to 20 chars
        (WhatsApp interactive-button limits)."""
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "to": normalize_phone_number(to),
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": bid, "title": title[:20]}}
                            for bid, title in buttons[:3]
                        ]
                    },
                },
            }
        )

    async def send_list(
        self,
        to: str,
        body: str,
        button_text: str,
        sections: list[tuple[str, list[tuple[str, str, str]]]],
    ) -> dict:
        """sections: list of (section_title, [(row_id, row_title, row_description), ...]).
        Max 10 sections x 10 rows; section titles truncated to 24 chars, row
        titles to 24, descriptions to 72 (WhatsApp interactive-list limits)."""
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "to": normalize_phone_number(to),
                "type": "interactive",
                "interactive": {
                    "type": "list",
                    "body": {"text": body},
                    "action": {
                        "button": button_text[:20],
                        "sections": [
                            {
                                "title": section_title[:24],
                                "rows": [
                                    {"id": rid, "title": rtitle[:24], "description": rdesc[:72]}
                                    for rid, rtitle, rdesc in rows[:10]
                                ],
                            }
                            for section_title, rows in sections[:10]
                        ],
                    },
                },
            }
        )

    async def mark_as_read(self, whatsapp_message_id: str) -> dict:
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": whatsapp_message_id,
            }
        )
