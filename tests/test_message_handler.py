"""_notify_merchant: the proactive WhatsApp message sent to the merchant when
something needs their attention (e.g. a new pending order), independent of
whatever reply just went to the customer."""

import pytest
from sqlalchemy import select

from app.models import Business, Contact, Conversation, Message
from app.services.whatsapp.message_handler import _notify_merchant

pytestmark = pytest.mark.asyncio


class FakeWhatsAppClient:
    def __init__(self):
        self.sent = []

    async def send_text(self, to, body):
        self.sent.append((to, body))
        return {"messages": [{"id": "wamid.FAKE"}]}


async def _make_business(db_session, owner_whatsapp_number="221700000041") -> Business:
    business = Business(
        name="Boutique Test", whatsapp_number="221700000040", owner_whatsapp_number=owner_whatsapp_number
    )
    db_session.add(business)
    await db_session.flush()
    return business


async def test_notify_merchant_sends_and_logs_in_their_conversation(db_session):
    business = await _make_business(db_session)
    client = FakeWhatsAppClient()

    await _notify_merchant(db_session, client, business, "🔔 Nouvelle commande CMD-X de Amadou — 1000 FCFA.")

    assert len(client.sent) == 1
    to, body = client.sent[0]
    assert to == business.owner_whatsapp_number
    assert "Nouvelle commande" in body

    contact = await db_session.scalar(
        select(Contact).where(Contact.business_id == business.id, Contact.wa_id == business.owner_whatsapp_number)
    )
    assert contact is not None

    conversation = await db_session.scalar(select(Conversation).where(Conversation.contact_id == contact.id))
    assert conversation is not None
    assert conversation.message_count == 1

    message = await db_session.scalar(select(Message).where(Message.conversation_id == conversation.id))
    assert message is not None
    assert "Nouvelle commande" in message.content


async def test_notify_merchant_reuses_existing_conversation_on_second_call(db_session):
    business = await _make_business(db_session)
    client = FakeWhatsAppClient()

    await _notify_merchant(db_session, client, business, "Première notification")
    await _notify_merchant(db_session, client, business, "Deuxième notification")

    assert len(client.sent) == 2
    contacts = (
        await db_session.execute(
            select(Contact).where(Contact.business_id == business.id, Contact.wa_id == business.owner_whatsapp_number)
        )
    ).scalars().all()
    assert len(contacts) == 1  # not duplicated

    conversations = (
        await db_session.execute(select(Conversation).where(Conversation.contact_id == contacts[0].id))
    ).scalars().all()
    assert len(conversations) == 1
    assert conversations[0].message_count == 2


async def test_notify_merchant_is_a_noop_without_an_owner_number(db_session):
    business = await _make_business(db_session, owner_whatsapp_number="")
    client = FakeWhatsAppClient()

    await _notify_merchant(db_session, client, business, "test")

    assert client.sent == []
