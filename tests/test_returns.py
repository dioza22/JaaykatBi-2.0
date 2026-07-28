import pytest
from sqlalchemy import select

from app.models import Business, Contact, Conversation, DeliveryType, Order, OrderStatus, PaymentMethod, Product
from app.services.conversation.engine import handle_message
from app.services.orders.service import confirm_order, create_order, fulfill_order

pytestmark = pytest.mark.asyncio


async def _setup(db_session):
    business = Business(
        name="Boutique Test",
        whatsapp_number="221700000040",
        owner_whatsapp_number="221700000041",
    )
    db_session.add(business)
    await db_session.flush()

    product = Product(
        business_id=business.id, name="Riz parfumé 5 kg", price_xof=4500,
        track_inventory=True, quantity_in_stock=10,
    )
    db_session.add(product)

    customer = Contact(business_id=business.id, wa_id="221770001111", phone_number="221770001111")
    merchant_contact = Contact(
        business_id=business.id, wa_id=business.owner_whatsapp_number, phone_number=business.owner_whatsapp_number
    )
    db_session.add_all([customer, merchant_contact])
    await db_session.flush()

    customer_conversation = Conversation(business_id=business.id, contact_id=customer.id, state={})
    merchant_conversation = Conversation(business_id=business.id, contact_id=merchant_contact.id, state={})
    db_session.add_all([customer_conversation, merchant_conversation])
    await db_session.flush()

    return business, product, customer, customer_conversation, merchant_contact, merchant_conversation


async def _make_fulfilled_order(db_session, business, product, customer):
    order = await create_order(
        db_session, business, customer, product, 1, "Client Test",
        DeliveryType.PICKUP, None, PaymentMethod.CASH,
    )
    await confirm_order(order)
    await fulfill_order(order)
    await db_session.flush()
    return order


def _row_titles(reply):
    return [row[1] for section in reply.list_sections for row in section[1]]


def _button_titles(reply):
    return [title for _id, title in reply.buttons]


async def _customer_says(db_session, business, contact, conversation, text):
    return await handle_message(db_session, business, contact, conversation, text, is_merchant=False)


async def _merchant_says(db_session, business, contact, conversation, text):
    return await handle_message(db_session, business, contact, conversation, text, is_merchant=True)


async def test_customer_return_request_flow_notifies_merchant(db_session):
    business, product, customer, customer_conv, _merchant_contact, _merchant_conv = await _setup(db_session)
    order = await _make_fulfilled_order(db_session, business, product, customer)

    reply = await _customer_says(db_session, business, customer, customer_conv, "je veux un remboursement")
    assert "numéro" in reply.text.lower()  # no list shown — customer must type their own order number
    assert "Annuler" in _button_titles(reply)

    reply = await _customer_says(db_session, business, customer, customer_conv, order.order_number)
    assert "confirmez-vous" in reply.text.lower()
    assert set(_button_titles(reply)) == {"Confirmer", "Annuler"}

    reply = await _customer_says(db_session, business, customer, customer_conv, "confirmer")
    assert order.order_number in reply.text
    assert reply.merchant_notification is not None
    assert order.order_number in reply.merchant_notification.text
    assert set(title for _id, title in reply.merchant_notification.buttons) == {
        "Accepter le retour", "Ignorer la demande", "Retour",
    }
    assert reply.merchant_notification.order_number == order.order_number
    assert customer_conv.state["flow"] is None

    await db_session.flush()
    await db_session.refresh(order)
    assert order.return_requested_at is not None
    assert order.status == OrderStatus.FULFILLED  # not yet accepted — merchant still has to act


async def test_customer_return_request_excludes_already_requested_orders(db_session):
    business, product, customer, customer_conv, _mc, _mconv = await _setup(db_session)
    order = await _make_fulfilled_order(db_session, business, product, customer)
    order.request_return()
    await db_session.flush()

    reply = await _customer_says(db_session, business, customer, customer_conv, "je veux un remboursement")
    assert "n'avez pas de commande" in reply.text.lower()
    assert customer_conv.state.get("flow") is None


async def test_customer_return_request_with_no_fulfilled_orders(db_session):
    business, product, customer, customer_conv, _mc, _mconv = await _setup(db_session)

    reply = await _customer_says(db_session, business, customer, customer_conv, "je veux un remboursement")
    assert "n'avez pas de commande" in reply.text.lower()
    assert customer_conv.state.get("flow") is None


async def test_customer_return_request_rejects_wrong_order_number(db_session):
    business, product, customer, customer_conv, _mc, _mconv = await _setup(db_session)
    await _make_fulfilled_order(db_session, business, product, customer)

    await _customer_says(db_session, business, customer, customer_conv, "je veux un remboursement")
    reply = await _customer_says(db_session, business, customer, customer_conv, "CMD-99999999-9999")

    assert "introuvable" in reply.text.lower()
    assert customer_conv.state["flow"] == "demander_retour"  # still in the flow, can retry
    assert customer_conv.state["step"] == 0


async def test_merchant_orders_list_grouped_by_status_with_fulfilled_last(db_session):
    business, product, customer, _cc, merchant_contact, merchant_conv = await _setup(db_session)

    pending_order = await create_order(
        db_session, business, customer, product, 1, "Client A", DeliveryType.PICKUP, None, PaymentMethod.CASH,
    )
    confirmed_order = await create_order(
        db_session, business, customer, product, 1, "Client B", DeliveryType.PICKUP, None, PaymentMethod.CASH,
    )
    await confirm_order(confirmed_order)
    fulfilled_order = await _make_fulfilled_order(db_session, business, product, customer)
    await db_session.flush()

    reply = await _merchant_says(db_session, business, merchant_contact, merchant_conv, "mes commandes")

    section_labels = [section[0] for section in reply.list_sections]
    assert section_labels == ["En attente", "Confirmées", "Livrées", "Autre"]

    row_titles = _row_titles(reply)
    assert row_titles.index(fulfilled_order.order_number) > row_titles.index(pending_order.order_number)
    assert row_titles.index(fulfilled_order.order_number) > row_titles.index(confirmed_order.order_number)
    assert pending_order.order_number in row_titles
    assert confirmed_order.order_number in row_titles


async def test_merchant_fulfilled_order_without_return_request_is_not_actionable(db_session):
    business, product, customer, _cc, merchant_contact, merchant_conv = await _setup(db_session)
    order = await _make_fulfilled_order(db_session, business, product, customer)

    await _merchant_says(db_session, business, merchant_contact, merchant_conv, "mes commandes")
    reply = await _merchant_says(db_session, business, merchant_contact, merchant_conv, order.order_number)

    assert "aucune action requise" in reply.text.lower()
    assert _button_titles(reply) == ["Retour"]


async def test_merchant_can_accept_a_return_request(db_session):
    business, product, customer, _cc, merchant_contact, merchant_conv = await _setup(db_session)
    order = await _make_fulfilled_order(db_session, business, product, customer)
    order.request_return()
    await db_session.flush()

    await _merchant_says(db_session, business, merchant_contact, merchant_conv, "mes commandes")
    reply = await _merchant_says(db_session, business, merchant_contact, merchant_conv, order.order_number)
    assert set(_button_titles(reply)) == {"Accepter le retour", "Ignorer la demande", "Retour"}
    assert "retour demandé" in reply.text.lower()

    reply = await _merchant_says(db_session, business, merchant_contact, merchant_conv, "Accepter le retour")
    assert "retournée" in reply.text.lower() or "remboursée" in reply.text.lower()
    assert merchant_conv.state.get("flow") is None

    await db_session.flush()
    await db_session.refresh(order)
    assert order.status == OrderStatus.RETURNED


async def test_merchant_can_dismiss_a_return_request(db_session):
    business, product, customer, _cc, merchant_contact, merchant_conv = await _setup(db_session)
    order = await _make_fulfilled_order(db_session, business, product, customer)
    order.request_return()
    await db_session.flush()

    await _merchant_says(db_session, business, merchant_contact, merchant_conv, "mes commandes")
    await _merchant_says(db_session, business, merchant_contact, merchant_conv, order.order_number)
    reply = await _merchant_says(db_session, business, merchant_contact, merchant_conv, "Ignorer la demande")
    assert "ignorée" in reply.text.lower()

    await db_session.flush()
    await db_session.refresh(order)
    assert order.status == OrderStatus.FULFILLED  # stays fulfilled
    assert order.return_requested_at is None  # cleared


async def test_sales_summary_excludes_returned_orders(db_session):
    business, product, customer, _cc, merchant_contact, merchant_conv = await _setup(db_session)
    order = await _make_fulfilled_order(db_session, business, product, customer)
    order.accept_return()
    await db_session.flush()

    reply = await _merchant_says(db_session, business, merchant_contact, merchant_conv, "mes ventes")
    assert "0 commande(s) pour 0 FCFA" in reply.text
