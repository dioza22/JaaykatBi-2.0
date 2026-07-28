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
from app.services.conversation import state
from app.services.conversation.engine import handle_message
from app.services.conversation.merchant_flows import VIEW_ORDERS_FLOW
from app.services.conversation.reply import MerchantNotification
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

    fields = [change.field for entry in payload.entry for change in entry.changes]
    inbound_count = sum(len(change.value.messages) for entry in payload.entry for change in entry.changes)
    status_count = sum(len(change.value.statuses) for entry in payload.entry for change in entry.changes)
    logger.info("webhook received: fields=%s messages=%d statuses=%d", fields, inbound_count, status_count)

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


async def _get_or_create_contact(
    db: AsyncSession, business: Business, wa_id: str, contact_name: str | None = None
) -> Contact:
    contact = await db.scalar(
        select(Contact).where(Contact.business_id == business.id, Contact.wa_id == wa_id)
    )
    if contact:
        if contact_name and not contact.display_name:
            contact.display_name = contact_name
        contact.update_last_contact()
        return contact

    contact = Contact(
        business_id=business.id,
        wa_id=wa_id,
        phone_number=wa_id,
        display_name=contact_name,
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
        logger.warning(
            "No business matched for inbound message: display_phone_number=%r normalized=%r wa_id=%r",
            inbound.business_whatsapp_number,
            normalize_phone_number(inbound.business_whatsapp_number),
            inbound.wa_id,
        )
        return  # unknown business number — nothing we can do

    contact = await _get_or_create_contact(db, business, inbound.wa_id, inbound.contact_name)
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

    if reply and reply.text:
        db.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.OUTBOUND,
                content=reply.text,
                was_ai_generated=False,
            )
        )
        try:
            if reply.list_sections:
                await whatsapp_client.send_list(
                    contact.wa_id, reply.text, reply.list_button_text or "Menu", reply.list_sections
                )
            elif reply.buttons:
                await whatsapp_client.send_buttons(contact.wa_id, reply.text, reply.buttons)
            else:
                await whatsapp_client.send_text(contact.wa_id, reply.text)
        except Exception:
            logger.warning("sending reply failed for conversation %s", conversation.id, exc_info=True)

    if reply and reply.merchant_notification:
        await _notify_merchant(db, whatsapp_client, business, reply.merchant_notification)

    if reply and reply.customer_notification:
        wa_id, text = reply.customer_notification
        await _send_notification(db, whatsapp_client, business, wa_id, text)


async def _send_notification(
    db: AsyncSession,
    whatsapp_client: WhatsAppClient,
    business: Business,
    wa_id: str | None,
    text: str,
    buttons: list[tuple[str, str]] | None = None,
) -> None:
    """A separate, proactive WhatsApp message — independent of whatever
    reply just went to whoever triggered it. Logged into the recipient's own
    conversation so it shows up in their history (and the LLM fallback's
    context) too."""
    if not wa_id:
        return
    contact = await _get_or_create_contact(db, business, wa_id)
    conversation = await _get_or_create_conversation(db, business, contact)
    db.add(
        Message(
            conversation_id=conversation.id,
            direction=MessageDirection.OUTBOUND,
            content=text,
            was_ai_generated=False,
        )
    )
    conversation.message_count += 1
    conversation.last_message_at = datetime.now(UTC)
    try:
        if buttons:
            await whatsapp_client.send_buttons(wa_id, text, buttons)
        else:
            await whatsapp_client.send_text(wa_id, text)
    except Exception:
        logger.warning("notification send failed for business %s wa_id %s", business.id, wa_id, exc_info=True)


async def _notify_merchant(
    db: AsyncSession, whatsapp_client: WhatsAppClient, business: Business, notification: MerchantNotification
) -> None:
    """Order-related notifications (a new pending order, a return request)
    carry action buttons — when they do, the merchant's own conversation is
    seeded to the "view this order" submenu first (same state
    _continue_view_orders would set after they pick the order from a list),
    so tapping a button resolves immediately. Only done when the merchant
    has no flow already in progress, so it never silently interrupts
    something else they were in the middle of (e.g. adding a product)."""
    if not business.owner_whatsapp_number:
        return

    text = notification.text
    buttons = notification.buttons
    if notification.order_number and notification.buttons:
        merchant_contact = await _get_or_create_contact(db, business, business.owner_whatsapp_number)
        merchant_conversation = await _get_or_create_conversation(db, business, merchant_contact)
        if state.current_flow(merchant_conversation) is None:
            state.start_flow(merchant_conversation, VIEW_ORDERS_FLOW)
            state.advance(merchant_conversation, 1, {"order_number": notification.order_number})
        else:
            # Busy with something else — showing buttons that wouldn't
            # resolve against that other flow would be worse than none.
            buttons = None
            text = f"{text} Répondez 'mes commandes' pour la traiter."

    await _send_notification(db, whatsapp_client, business, business.owner_whatsapp_number, text, buttons=buttons)
