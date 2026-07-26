"""Wires an inbound webhook payload to persistence + the conversation engine.
Mirrors the old C# MessageHandler.cs: resolve business/contact/conversation,
persist the inbound message, mark it read, delegate to the conversation
engine for a reply, persist + send the reply."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, Contact, Conversation, ConversationStatus, Message, MessageDirection, MessageType
from app.schemas.whatsapp import InboundMessage, WAStatus, WhatsAppWebhookPayload
from app.services.conversation.engine import handle_message
from app.services.whatsapp.client import WhatsAppClient, normalize_phone_number
from app.services.whatsapp.webhook_parser import parse_inbound_messages, parse_statuses

logger = logging.getLogger(__name__)

_MESSAGE_TYPE_MAP = {
    "text": MessageType.TEXT,
    "image": MessageType.IMAGE,
    "audio": MessageType.AUDIO,
    "video": MessageType.VIDEO,
    "document": MessageType.DOCUMENT,
    "location": MessageType.LOCATION,
    "contacts": MessageType.CONTACT,
    "interactive": MessageType.INTERACTIVE,
}

_STATUS_MAP = {
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
}


async def process_webhook(db: AsyncSession, whatsapp_client: WhatsAppClient, payload: WhatsAppWebhookPayload) -> None:
    if payload.object != "whatsapp_business_account":
        return

    for status in parse_statuses(payload):
        await _update_message_status(db, status)

    for message in parse_inbound_messages(payload):
        await _handle_inbound(db, whatsapp_client, message)

    await db.commit()


async def _update_message_status(db: AsyncSession, status: WAStatus) -> None:
    from app.models import MessageStatus

    new_status = {
        "sent": MessageStatus.SENT,
        "delivered": MessageStatus.DELIVERED,
        "read": MessageStatus.READ,
        "failed": MessageStatus.FAILED,
    }.get(status.status)
    if new_status is None:
        return
    row = await db.scalar(select(Message).where(Message.whatsapp_message_id == status.id))
    if row:
        row.status = new_status


async def _get_or_create_business(db: AsyncSession, inbound: InboundMessage) -> Business | None:
    number = normalize_phone_number(inbound.business_whatsapp_number)
    return await db.scalar(select(Business).where(Business.whatsapp_number == number))


async def _get_or_create_contact(db: AsyncSession, business: Business, inbound: InboundMessage) -> Contact:
    contact = await db.scalar(
        select(Contact).where(Contact.business_id == business.id, Contact.wa_id == inbound.wa_id)
    )
    if contact:
        if inbound.contact_name and not contact.display_name:
            contact.display_name = inbound.contact_name
        contact.update_last_contact()
        return contact

    contact = Contact(
        business_id=business.id,
        wa_id=inbound.wa_id,
        phone_number=inbound.wa_id,
        display_name=inbound.contact_name,
        last_contact_at=datetime.now(UTC),
    )
    db.add(contact)
    await db.flush()
    return contact


async def _get_or_create_conversation(db: AsyncSession, business: Business, contact: Contact) -> Conversation:
    conversation = await db.scalar(
        select(Conversation)
        .where(
            Conversation.business_id == business.id,
            Conversation.contact_id == contact.id,
            Conversation.status == ConversationStatus.ACTIVE,
        )
        .order_by(Conversation.started_at.desc())
    )
    now = datetime.now(UTC)
    if conversation and conversation.last_message_at:
        last = (
            conversation.last_message_at
            if conversation.last_message_at.tzinfo
            else conversation.last_message_at.replace(tzinfo=UTC)
        )
        if now - last > timedelta(hours=24):
            conversation.status = ConversationStatus.CLOSED
            conversation.closed_at = now
            conversation = None

    if conversation is None:
        conversation = Conversation(business_id=business.id, contact_id=contact.id, state={})
        db.add(conversation)
        await db.flush()

    return conversation


async def _handle_inbound(db: AsyncSession, whatsapp_client: WhatsAppClient, inbound: InboundMessage) -> None:
    business = await _get_or_create_business(db, inbound)
    if business is None:
        return  # unknown business number — nothing we can do

    contact = await _get_or_create_contact(db, business, inbound)
    conversation = await _get_or_create_conversation(db, business, contact)

    db.add(
        Message(
            conversation_id=conversation.id,
            direction=MessageDirection.INBOUND,
            message_type=_MESSAGE_TYPE_MAP.get(inbound.message_type, MessageType.TEXT),
            content=inbound.text,
            whatsapp_message_id=inbound.whatsapp_message_id,
        )
    )
    conversation.message_count += 1
    conversation.last_message_at = datetime.now(UTC)

    try:
        await whatsapp_client.mark_as_read(inbound.whatsapp_message_id)
    except Exception:
        logger.warning("mark_as_read failed for %s", inbound.whatsapp_message_id, exc_info=True)

    is_merchant = inbound.wa_id == business.owner_whatsapp_number
    reply = await handle_message(db, business, contact, conversation, inbound.text, is_merchant=is_merchant)

    if reply:
        db.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.OUTBOUND,
                content=reply,
                was_ai_generated=False,
            )
        )
        try:
            await whatsapp_client.send_text(contact.wa_id, reply)
        except Exception:
            logger.warning("send_text failed for conversation %s", conversation.id, exc_info=True)
