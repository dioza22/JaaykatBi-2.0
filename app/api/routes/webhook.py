import logging

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response

from app.config import get_settings
from app.db import SessionLocal
from app.schemas.whatsapp import WhatsAppWebhookPayload
from app.services.whatsapp.client import WhatsAppClient
from app.services.whatsapp.message_handler import process_webhook

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/api/webhook/whatsapp")
async def verify_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
) -> Response:
    settings = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_webhook_verify_token:
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(status_code=403)


async def _process_in_background(raw_body: dict) -> None:
    """Runs after the request has already returned 200 to Meta, in its own DB
    session (the request-scoped session is gone by the time this runs)."""
    try:
        payload = WhatsAppWebhookPayload.model_validate(raw_body)
        async with SessionLocal() as db:
            client = WhatsAppClient()
            await process_webhook(db, client, payload)
    except Exception:
        logger.exception("Failed to process WhatsApp webhook payload")


@router.post("/api/webhook/whatsapp")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    raw_body = await request.json()
    background_tasks.add_task(_process_in_background, raw_body)
    return {"status": "received"}
