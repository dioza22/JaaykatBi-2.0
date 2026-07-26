import pytest
from sqlalchemy import select

from app.models import Business, Contact, Conversation, Order, OrderStatus, Product
from app.services.conversation.engine import handle_message

pytestmark = pytest.mark.asyncio


async def _setup(db_session):
    business = Business(
        name="Boutique Test",
        whatsapp_number="221700000020",
        owner_whatsapp_number="221700000021",
        welcome_message="Bienvenue !",
    )
    db_session.add(business)
    await db_session.flush()

    product = Product(
        business_id=business.id, name="Riz parfumé 5 kg", price_xof=4500,
        track_inventory=True, quantity_in_stock=10,
    )
    db_session.add(product)

    contact = Contact(business_id=business.id, wa_id="221770001111", phone_number="221770001111")
    db_session.add(contact)
    await db_session.flush()

    conversation = Conversation(business_id=business.id, contact_id=contact.id, state={})
    db_session.add(conversation)
    await db_session.flush()

    return business, product, contact, conversation


async def test_full_order_flow_creates_order_and_deducts_stock(db_session):
    business, product, contact, conversation = await _setup(db_session)

    reply = await handle_message(db_session, business, contact, conversation, "Je veux commander", is_merchant=False)
    assert "Riz parfumé" in reply

    reply = await handle_message(db_session, business, contact, conversation, "1", is_merchant=False)
    assert "Combien d'unités" in reply

    reply = await handle_message(db_session, business, contact, conversation, "2", is_merchant=False)
    assert "nom complet" in reply

    reply = await handle_message(db_session, business, contact, conversation, "Amadou Fall", is_merchant=False)
    assert "livraison" in reply.lower()

    reply = await handle_message(db_session, business, contact, conversation, "retrait", is_merchant=False)
    assert "payer" in reply.lower()

    reply = await handle_message(db_session, business, contact, conversation, "3", is_merchant=False)
    assert "Récapitulatif" in reply
    assert "9000" in reply  # 2 x 4500

    reply = await handle_message(db_session, business, contact, conversation, "confirmer", is_merchant=False)
    assert "référence CMD-" in reply

    order = (await db_session.execute(select(Order).where(Order.business_id == business.id))).scalar_one()
    assert order.status == OrderStatus.PENDING
    assert float(order.total_xof) == 9000.0
    assert order.customer_name == "Amadou Fall"

    await db_session.refresh(product)
    assert product.quantity_in_stock == 8  # 10 - 2

    await db_session.refresh(contact)
    assert contact.total_orders == 1
    assert float(contact.total_spent_xof) == 9000.0

    # Flow should be cleared after completion
    assert conversation.state["flow"] is None


async def test_order_flow_can_be_cancelled_mid_flow(db_session):
    business, product, contact, conversation = await _setup(db_session)

    await handle_message(db_session, business, contact, conversation, "commander", is_merchant=False)
    await handle_message(db_session, business, contact, conversation, "1", is_merchant=False)
    reply = await handle_message(db_session, business, contact, conversation, "annuler", is_merchant=False)

    assert "annulé" in reply.lower()
    assert conversation.state["flow"] is None

    orders = (await db_session.execute(select(Order).where(Order.business_id == business.id))).scalars().all()
    assert orders == []

    await db_session.refresh(product)
    assert product.quantity_in_stock == 10  # untouched


async def test_order_flow_rejects_quantity_exceeding_stock(db_session):
    business, product, contact, conversation = await _setup(db_session)

    await handle_message(db_session, business, contact, conversation, "commander", is_merchant=False)
    await handle_message(db_session, business, contact, conversation, "1", is_merchant=False)
    reply = await handle_message(db_session, business, contact, conversation, "50", is_merchant=False)

    assert "ne reste que" in reply.lower()
    # still awaiting quantity — flow not advanced
    assert conversation.state["step"] == 1
