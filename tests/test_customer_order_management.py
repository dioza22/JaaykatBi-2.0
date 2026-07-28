from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import (
    Business,
    Contact,
    Conversation,
    DeliveryType,
    Order,
    OrderStatus,
    PaymentMethod,
    Product,
)
from app.services.conversation.engine import handle_message
from app.services.orders.service import confirm_order, create_order

pytestmark = pytest.mark.asyncio


async def _setup(db_session):
    business = Business(
        name="Boutique Test",
        whatsapp_number="221700000060",
        owner_whatsapp_number="221700000061",
    )
    db_session.add(business)
    await db_session.flush()

    product = Product(
        business_id=business.id, name="Riz parfumé 5 kg", price_xof=4500,
        track_inventory=True, quantity_in_stock=10,
    )
    db_session.add(product)

    customer = Contact(business_id=business.id, wa_id="221770002222", phone_number="221770002222")
    db_session.add(customer)
    await db_session.flush()

    conversation = Conversation(business_id=business.id, contact_id=customer.id, state={})
    db_session.add(conversation)
    await db_session.flush()

    return business, product, customer, conversation


async def _place_order(db_session, business, product, customer, quantity=1, delivery=False, address="Rue 1, Dakar"):
    return await create_order(
        db_session, business, customer, product, quantity, customer.display_name or "Client Test",
        DeliveryType.DELIVERY if delivery else DeliveryType.PICKUP,
        address if delivery else None,
        PaymentMethod.CASH,
    )


def _row_titles(reply):
    return [row[1] for section in reply.list_sections for row in section[1]]


def _section_labels(reply):
    return [section[0] for section in reply.list_sections]


def _button_titles(reply):
    return [title for _id, title in reply.buttons]


async def _customer_says(db_session, business, contact, conversation, text):
    return await handle_message(db_session, business, contact, conversation, text, is_merchant=False)


async def test_ongoing_orders_grouped_by_status_excludes_fulfilled(db_session):
    business, product, customer, conversation = await _setup(db_session)

    pending_order = await _place_order(db_session, business, product, customer)
    confirmed_order = await _place_order(db_session, business, product, customer)
    await confirm_order(confirmed_order)
    fulfilled_order = await _place_order(db_session, business, product, customer)
    await confirm_order(fulfilled_order)
    from app.services.orders.service import fulfill_order
    await fulfill_order(fulfilled_order)
    await db_session.flush()

    reply = await _customer_says(db_session, business, customer, conversation, "mes commandes")

    assert _section_labels(reply) == ["En attente", "Confirmées", "Autre"]
    row_titles = _row_titles(reply)
    assert pending_order.order_number in row_titles
    assert confirmed_order.order_number in row_titles
    assert fulfilled_order.order_number not in row_titles


async def test_ongoing_orders_list_never_exceeds_whatsapp_total_row_cap(db_session):
    """Regression test: WhatsApp's interactive list caps rows at 10 TOTAL
    across every section, not 10 per section — the bug that silently broke
    the merchant's grouped order list in production (error 131009, swallowed
    by message_handler's broad except, no reply ever sent)."""
    business, product, customer, conversation = await _setup(db_session)
    product.quantity_in_stock = 20
    await db_session.flush()

    for _ in range(6):
        await _place_order(db_session, business, product, customer)
    for _ in range(6):
        order = await _place_order(db_session, business, product, customer)
        await confirm_order(order)
    await db_session.flush()

    reply = await _customer_says(db_session, business, customer, conversation, "mes commandes")

    total_rows = sum(len(rows) for _label, rows in reply.list_sections)
    assert total_rows <= 10


async def test_ongoing_orders_empty_state(db_session):
    business, product, customer, conversation = await _setup(db_session)

    reply = await _customer_says(db_session, business, customer, conversation, "mes commandes")
    assert "aucune commande en cours" in reply.text.lower()
    assert conversation.state.get("flow") is None


async def test_ongoing_orders_submenu_shows_three_actions(db_session):
    business, product, customer, conversation = await _setup(db_session)
    order = await _place_order(db_session, business, product, customer)

    await _customer_says(db_session, business, customer, conversation, "mes commandes")
    reply = await _customer_says(db_session, business, customer, conversation, order.order_number)

    assert set(_button_titles(reply)) == {"Mettre à jour", "Annuler la commande", "Retour"}


async def test_ongoing_orders_retour_goes_back_to_the_list(db_session):
    business, product, customer, conversation = await _setup(db_session)
    order = await _place_order(db_session, business, product, customer)

    await _customer_says(db_session, business, customer, conversation, "mes commandes")
    await _customer_says(db_session, business, customer, conversation, order.order_number)
    reply = await _customer_says(db_session, business, customer, conversation, "Retour")

    assert order.order_number in _row_titles(reply)
    assert conversation.state["flow"] == "mes_commandes_client"
    assert conversation.state["step"] == 0


async def test_free_cancellation_within_24h_pending(db_session):
    business, product, customer, conversation = await _setup(db_session)
    order = await _place_order(db_session, business, product, customer)

    await _customer_says(db_session, business, customer, conversation, "mes commandes")
    await _customer_says(db_session, business, customer, conversation, order.order_number)
    reply = await _customer_says(db_session, business, customer, conversation, "Annuler la commande")

    assert "a été annulée" in reply.text
    assert reply.merchant_notification is not None
    assert conversation.state.get("flow") is None

    await db_session.flush()
    await db_session.refresh(order)
    assert order.status == OrderStatus.CANCELLED
    assert order.cancellation_fee_xof is None


async def test_late_cancellation_confirmed_order_discloses_configured_fee(db_session):
    business, product, customer, conversation = await _setup(db_session)
    business.late_cancellation_fee_percent = 10
    order = await _place_order(db_session, business, product, customer)
    await confirm_order(order)
    await db_session.flush()

    await _customer_says(db_session, business, customer, conversation, "mes commandes")
    await _customer_says(db_session, business, customer, conversation, order.order_number)
    reply = await _customer_says(db_session, business, customer, conversation, "Annuler la commande")

    assert "10%" in reply.text
    assert "450 FCFA" in reply.text  # 10% of 4500
    assert set(_button_titles(reply)) == {"Confirmer", "Annuler"}

    reply = await _customer_says(db_session, business, customer, conversation, "confirmer")
    assert "450 FCFA" in reply.text
    assert "450 FCFA" in reply.merchant_notification

    await db_session.flush()
    await db_session.refresh(order)
    assert order.status == OrderStatus.CANCELLED
    assert float(order.cancellation_fee_xof) == 450.0


async def test_late_cancellation_with_no_fee_configured_has_no_amount(db_session):
    business, product, customer, conversation = await _setup(db_session)
    order = await _place_order(db_session, business, product, customer)
    await confirm_order(order)
    await db_session.flush()

    await _customer_says(db_session, business, customer, conversation, "mes commandes")
    await _customer_says(db_session, business, customer, conversation, order.order_number)
    reply = await _customer_says(db_session, business, customer, conversation, "Annuler la commande")

    assert "FCFA" not in reply.text
    assert "délai de 24h" in reply.text

    reply = await _customer_says(db_session, business, customer, conversation, "confirmer")
    await db_session.flush()
    await db_session.refresh(order)
    assert order.status == OrderStatus.CANCELLED
    assert order.cancellation_fee_xof is None


async def test_late_cancellation_by_age_even_while_still_pending(db_session):
    business, product, customer, conversation = await _setup(db_session)
    order = await _place_order(db_session, business, product, customer)
    order.created_at = datetime.now(UTC) - timedelta(hours=25)
    await db_session.flush()

    await _customer_says(db_session, business, customer, conversation, "mes commandes")
    await _customer_says(db_session, business, customer, conversation, order.order_number)
    reply = await _customer_says(db_session, business, customer, conversation, "Annuler la commande")

    assert set(_button_titles(reply)) == {"Confirmer", "Annuler"}  # not an immediate free cancel


async def test_update_quantity_recomputes_stock_total_and_contact_spend(db_session):
    business, product, customer, conversation = await _setup(db_session)
    order = await _place_order(db_session, business, product, customer, quantity=2)  # stock 10 -> 8
    await db_session.flush()

    await _customer_says(db_session, business, customer, conversation, "mes commandes")
    await _customer_says(db_session, business, customer, conversation, order.order_number)
    reply = await _customer_says(db_session, business, customer, conversation, "Mettre à jour")
    assert set(_button_titles(reply)) == {"Quantité", "Annuler"}  # pickup order — no address option

    reply = await _customer_says(db_session, business, customer, conversation, "Quantité")
    assert "2" in reply.text

    reply = await _customer_says(db_session, business, customer, conversation, "5")
    assert "mise à jour" in reply.text.lower()
    assert "22500" in reply.text  # 5 x 4500
    assert reply.merchant_notification is not None
    assert conversation.state.get("flow") is None

    await db_session.flush()
    await db_session.refresh(order)
    await db_session.refresh(product)
    await db_session.refresh(customer)
    assert float(order.total_xof) == 22500.0
    assert product.quantity_in_stock == 5  # 10 - 5
    assert float(customer.total_spent_xof) == 22500.0


async def test_update_quantity_rejects_insufficient_stock_without_revealing_count(db_session):
    business, product, customer, conversation = await _setup(db_session)
    order = await _place_order(db_session, business, product, customer, quantity=1)

    await _customer_says(db_session, business, customer, conversation, "mes commandes")
    await _customer_says(db_session, business, customer, conversation, order.order_number)
    await _customer_says(db_session, business, customer, conversation, "Mettre à jour")
    reply = await _customer_says(db_session, business, customer, conversation, "Quantité")
    reply = await _customer_says(db_session, business, customer, conversation, "500")

    assert "n'est pas disponible" in reply.text
    assert "9" not in reply.text  # remaining stock (10 - 1) never revealed


async def test_update_address_only_offered_for_delivery_orders(db_session):
    business, product, customer, conversation = await _setup(db_session)
    order = await _place_order(db_session, business, product, customer, delivery=True, address="Rue 10, Dakar")

    await _customer_says(db_session, business, customer, conversation, "mes commandes")
    await _customer_says(db_session, business, customer, conversation, order.order_number)
    reply = await _customer_says(db_session, business, customer, conversation, "Mettre à jour")
    assert set(_button_titles(reply)) == {"Quantité", "Adresse", "Annuler"}

    reply = await _customer_says(db_session, business, customer, conversation, "Adresse")
    assert "Rue 10, Dakar" in reply.text

    reply = await _customer_says(db_session, business, customer, conversation, "Rue 20, Pikine")
    assert "mise à jour" in reply.text.lower()
    assert reply.merchant_notification is not None

    await db_session.flush()
    await db_session.refresh(order)
    assert order.delivery_address == "Rue 20, Pikine"
