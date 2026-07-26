from app.schemas.whatsapp import InboundMessage, WAMessage, WAStatus, WhatsAppWebhookPayload


def extract_message_text(message: WAMessage) -> str:
    if message.type == "text" and message.text:
        return message.text.body
    if message.type == "interactive" and message.interactive:
        reply = message.interactive.button_reply or message.interactive.list_reply
        if reply and reply.title:
            return reply.title
        return "[Interactive]"
    if message.type == "image":
        if message.image and message.image.caption:
            return message.image.caption
        return "[Image]"
    if message.type == "location" and message.location:
        parts = [p for p in (message.location.name, message.location.address) if p]
        return f"[Location: {', '.join(parts)}]" if parts else "[Location]"
    return f"[{message.type}]"


def parse_inbound_messages(payload: WhatsAppWebhookPayload) -> list[InboundMessage]:
    """Flattens every inbound message across entries/changes into a simple list.
    Ignores status-only changes (see parse_statuses)."""
    results: list[InboundMessage] = []
    for entry in payload.entry:
        for change in entry.changes:
            if change.field != "messages":
                continue
            value = change.value
            if not value.messages:
                continue
            names_by_wa_id = {c.wa_id: (c.profile.name if c.profile else None) for c in value.contacts}
            for message in value.messages:
                results.append(
                    InboundMessage(
                        business_whatsapp_number=value.metadata.display_phone_number,
                        phone_number_id=value.metadata.phone_number_id,
                        wa_id=message.from_,
                        contact_name=names_by_wa_id.get(message.from_),
                        whatsapp_message_id=message.id,
                        message_type=message.type,
                        text=extract_message_text(message),
                    )
                )
    return results


def parse_statuses(payload: WhatsAppWebhookPayload) -> list[WAStatus]:
    """Delivery receipts (delivered/read/failed) — used to update Message.status."""
    results: list[WAStatus] = []
    for entry in payload.entry:
        for change in entry.changes:
            if change.field != "messages":
                continue
            results.extend(change.value.statuses)
    return results
