"""_notify_merchant: the proactive WhatsApp message sent to the merchant when
something needs their attention (e.g. a new pending order), independent of
whatever reply just went to the customer."""

import pytest
from sqlalchemy import select

from app.models import Business, Contact, Conversation, Message
from app.services.conversation import state
from app.services.conversation.reply import MerchantNotification
from app.services.whatsapp.message_handler import _get_or_create_contact, _get_or_create_conversation, _notify_merchant

pytestmark = pytest.mark.asyncio


class FakeWhatsAppClient:
    def __init__(self):
        self.sent = []  # [(kind, to, body)] — kind in {"text", "buttons"}

    async def send_text(self, to, body):
        self.sent.append(("text", to, body))
        return {"messages": [{"id": "wamid.FAKE"}]}

    async def send_buttons(self, to, body, buttons):
        self.sent.append(("buttons", to, body))
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

    await _notify_merchant(
        db_session, client, business, MerchantNotification(text="🔔 Nouvelle commande CMD-X de Amadou — 1000 FCFA.")
    )

    assert len(client.sent) == 1
    kind, to, body = client.sent[0]
    assert kind == "text"
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

    await _notify_merchant(db_session, client, business, MerchantNotification(text="Première notification"))
    await _notify_merchant(db_session, client, business, MerchantNotification(text="Deuxième notification"))

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

    await _notify_merchant(db_session, client, business, MerchantNotification(text="test"))

    assert client.sent == []


async def test_notify_merchant_with_order_number_seeds_view_orders_state_when_idle(db_session):
    business = await _make_business(db_session)
    client = FakeWhatsAppClient()

    await _notify_merchant(
        db_session,
        client,
        business,
        MerchantNotification(
            text="🔔 Nouvelle commande CMD-1 — 1000 FCFA.",
            buttons=[("confirm", "Confirmer"), ("cancel_order", "Annuler la commande"), ("back", "Retour")],
            order_number="CMD-1",
        ),
    )

    assert len(client.sent) == 1
    kind, _to, _body = client.sent[0]
    assert kind == "buttons"  # merchant was idle — the interactive submenu goes out

    merchant_contact = await _get_or_create_contact(db_session, business, business.owner_whatsapp_number)
    merchant_conversation = await _get_or_create_conversation(db_session, business, merchant_contact)
    assert state.current_flow(merchant_conversation) == "voir_commandes"
    assert state.current_step(merchant_conversation) == 1
    assert state.current_slots(merchant_conversation) == {"order_number": "CMD-1"}


async def test_notify_merchant_with_order_number_falls_back_to_text_when_merchant_busy(db_session):
    business = await _make_business(db_session)
    client = FakeWhatsAppClient()

    # Merchant is mid-way through some other flow (e.g. adding a product)
    merchant_contact = await _get_or_create_contact(db_session, business, business.owner_whatsapp_number)
    merchant_conversation = await _get_or_create_conversation(db_session, business, merchant_contact)
    state.start_flow(merchant_conversation, "ajouter_produit")

    await _notify_merchant(
        db_session,
        client,
        business,
        MerchantNotification(
            text="🔔 Nouvelle commande CMD-2 — 2000 FCFA.",
            buttons=[("confirm", "Confirmer"), ("cancel_order", "Annuler la commande"), ("back", "Retour")],
            order_number="CMD-2",
        ),
    )

    assert len(client.sent) == 1
    kind, _to, body = client.sent[0]
    assert kind == "text"  # buttons would've been dropped into the wrong flow — plain text instead
    assert "mes commandes" in body.lower()  # hint to reach it once free

    # the merchant's in-progress task was NOT silently hijacked
    assert state.current_flow(merchant_conversation) == "ajouter_produit"
