import httpx
import pytest

from app.main import app
from app.schemas.whatsapp import WhatsAppWebhookPayload
from app.services.whatsapp.client import normalize_phone_number
from app.services.whatsapp.webhook_parser import parse_inbound_messages, parse_statuses

SAMPLE_TEXT_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "ENTRY_ID",
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "221771234567",
                            "phone_number_id": "PHONE_NUMBER_ID",
                        },
                        "contacts": [{"profile": {"name": "Amadou Fall"}, "wa_id": "221770001111"}],
                        "messages": [
                            {
                                "from": "221770001111",
                                "id": "wamid.ABCD1234",
                                "timestamp": "1700000000",
                                "type": "text",
                                "text": {"body": "Je veux commander du riz"},
                            }
                        ],
                    },
                }
            ],
        }
    ],
}

SAMPLE_STATUS_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "ENTRY_ID",
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "metadata": {
                            "display_phone_number": "221771234567",
                            "phone_number_id": "PHONE_NUMBER_ID",
                        },
                        "statuses": [
                            {
                                "id": "wamid.ABCD1234",
                                "status": "delivered",
                                "timestamp": "1700000001",
                                "recipient_id": "221770001111",
                            }
                        ],
                    },
                }
            ],
        }
    ],
}


# Meta also sends webhooks for other subscribed fields (account alerts,
# template status, etc.) with a value shape that has no metadata at all —
# a real one seen in testing looked like {"entity_type": "WABA", ...}. This
# must not blow up validation for the whole payload.
ACCOUNT_LEVEL_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "ENTRY_ID",
            "changes": [
                {
                    "field": "account_update",
                    "value": {"entity_type": "WABA", "some_other_field": "no status"},
                }
            ],
        }
    ],
}


def test_parse_inbound_messages_extracts_text():
    payload = WhatsAppWebhookPayload.model_validate(SAMPLE_TEXT_PAYLOAD)
    messages = parse_inbound_messages(payload)
    assert len(messages) == 1
    msg = messages[0]
    assert msg.wa_id == "221770001111"
    assert msg.contact_name == "Amadou Fall"
    assert msg.text == "Je veux commander du riz"
    assert msg.business_whatsapp_number == "221771234567"


def test_parse_statuses_extracts_delivery_receipt():
    payload = WhatsAppWebhookPayload.model_validate(SAMPLE_STATUS_PAYLOAD)
    statuses = parse_statuses(payload)
    assert len(statuses) == 1
    assert statuses[0].status == "delivered"
    assert statuses[0].id == "wamid.ABCD1234"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("770001111", "221770001111"),
        ("221770001111", "221770001111"),
        ("+221 77 000 11 11", "221770001111"),
    ],
)
def test_normalize_phone_number(raw, expected):
    assert normalize_phone_number(raw) == expected


async def test_webhook_verify_challenge():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/api/webhook/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "jaaykatbi_webhook_2026",
                "hub.challenge": "12345",
            },
        )
    assert resp.status_code == 200
    assert resp.text == "12345"


async def test_webhook_verify_rejects_wrong_token():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/api/webhook/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong",
                "hub.challenge": "12345",
            },
        )
    assert resp.status_code == 403


def test_account_level_payload_does_not_crash_parsing():
    payload = WhatsAppWebhookPayload.model_validate(ACCOUNT_LEVEL_PAYLOAD)
    assert parse_inbound_messages(payload) == []
    assert parse_statuses(payload) == []


async def test_webhook_post_returns_immediately():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/webhook/whatsapp", json=SAMPLE_STATUS_PAYLOAD)
    assert resp.status_code == 200
    assert resp.json() == {"status": "received"}
