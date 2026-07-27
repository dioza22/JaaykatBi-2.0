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
    Promotion,
)
from app.services.conversation.engine import handle_message
from app.services.conversation import state
from app.services.orders.service import create_order

pytestmark = pytest.mark.asyncio


async def _setup(db_session):
    business = Business(
        name="Boutique Test",
        whatsapp_number="221700000030",
        owner_whatsapp_number="221700000031",
    )
    db_session.add(business)
    await db_session.flush()

    product = Product(
        business_id=business.id, name="Riz parfumé 5 kg", price_xof=4500,
        track_inventory=True, quantity_in_stock=10,
    )
    db_session.add(product)

    merchant_contact = Contact(
        business_id=business.id, wa_id=business.owner_whatsapp_number, phone_number=business.owner_whatsapp_number
    )
    db_session.add(merchant_contact)
    await db_session.flush()

    conversation = Conversation(business_id=business.id, contact_id=merchant_contact.id, state={})
    db_session.add(conversation)
    await db_session.flush()

    return business, product, merchant_contact, conversation


async def _merchant_says(db_session, business, contact, conversation, text):
    return await handle_message(db_session, business, contact, conversation, text, is_merchant=True)


def _row_titles(reply):
    return [row[1] for section in reply.list_sections for row in section[1]]


def _button_titles(reply):
    return [title for _id, title in reply.buttons]


async def test_add_product_flow_creates_product(db_session):
    business, _, contact, conversation = await _setup(db_session)

    reply = await _merchant_says(db_session, business, contact, conversation, "ajouter un produit")
    assert "nom" in reply.text.lower()
    assert "Annuler" in _button_titles(reply)

    reply = await _merchant_says(db_session, business, contact, conversation, "Huile d'arachide 1L")
    assert "catégorie" in reply.text.lower()

    reply = await _merchant_says(db_session, business, contact, conversation, "-")
    assert "prix" in reply.text.lower()

    reply = await _merchant_says(db_session, business, contact, conversation, "2500")
    assert "stock" in reply.text.lower()

    reply = await _merchant_says(db_session, business, contact, conversation, "30")
    assert "Récapitulatif" in reply.text
    assert set(_button_titles(reply)) == {"Confirmer", "Annuler"}

    reply = await _merchant_says(db_session, business, contact, conversation, "confirmer")
    assert "ajouté au catalogue" in reply.text
    assert len(_row_titles(reply)) == 8  # merchant menu re-attached

    product = await db_session.scalar(select(Product).where(Product.name == "Huile d'arachide 1L"))
    assert product is not None
    assert float(product.price_xof) == 2500.0
    assert product.quantity_in_stock == 30
    assert conversation.state["flow"] is None


async def test_edit_product_price_flow(db_session):
    business, product, contact, conversation = await _setup(db_session)

    reply = await _merchant_says(db_session, business, contact, conversation, "modifier un produit")
    assert product.name in _row_titles(reply)
    assert "Annuler" in _row_titles(reply)

    reply = await _merchant_says(db_session, business, contact, conversation, "1")
    assert "modifier" in reply.text.lower()
    assert set(_row_titles(reply)) == {"Prix", "Stock", "Disponibilité", "Annuler"}

    reply = await _merchant_says(db_session, business, contact, conversation, "1")  # choose "Prix"
    assert "nouveau prix" in reply.text.lower()

    reply = await _merchant_says(db_session, business, contact, conversation, "5000")
    assert "désormais 5000" in reply.text

    await db_session.flush()
    await db_session.refresh(product)
    assert float(product.price_xof) == 5000.0
    assert conversation.state["flow"] is None


async def test_edit_product_stock_updates_initial_stock_baseline(db_session):
    business, product, contact, conversation = await _setup(db_session)
    product.quantity_in_stock = 3
    product.initial_stock = 50  # started with 50, 3 left — a restock should reset this baseline
    await db_session.flush()

    await _merchant_says(db_session, business, contact, conversation, "modifier un produit")
    await _merchant_says(db_session, business, contact, conversation, "1")
    await _merchant_says(db_session, business, contact, conversation, "Stock")
    await _merchant_says(db_session, business, contact, conversation, "40")

    await db_session.flush()
    await db_session.refresh(product)
    assert product.quantity_in_stock == 40
    assert product.initial_stock == 40  # restocking resets the baseline, not just the current count


async def test_delete_product_flow_deactivates_instead_of_hard_delete(db_session):
    business, product, contact, conversation = await _setup(db_session)

    reply = await _merchant_says(db_session, business, contact, conversation, "supprimer un produit")
    assert "Annuler" in _row_titles(reply)
    reply = await _merchant_says(db_session, business, contact, conversation, "1")
    assert "confirmez-vous" in reply.text.lower()
    assert set(_button_titles(reply)) == {"Oui", "Non", "Annuler"}

    reply = await _merchant_says(db_session, business, contact, conversation, "oui")
    assert "retiré du catalogue" in reply.text

    await db_session.flush()
    await db_session.refresh(product)
    assert product.is_available is False
    # row still exists (soft delete, so historical OrderItems stay valid)
    still_there = await db_session.get(Product, product.id)
    assert still_there is not None


async def test_launch_promotion_flow_creates_promotion(db_session):
    business, product, contact, conversation = await _setup(db_session)

    reply = await _merchant_says(db_session, business, contact, conversation, "lancer une promotion")
    assert "Annuler" in _row_titles(reply)
    await _merchant_says(db_session, business, contact, conversation, "1")
    reply = await _merchant_says(db_session, business, contact, conversation, "10")
    assert set(_row_titles(reply)) == {"3 jours", "7 jours", "14 jours", "Annuler"}

    reply = await _merchant_says(db_session, business, contact, conversation, "7")
    assert set(_button_titles(reply)) == {"Confirmer", "Annuler"}

    reply = await _merchant_says(db_session, business, contact, conversation, "confirmer")
    assert "Promotion activée" in reply.text

    promo = await db_session.scalar(select(Promotion).where(Promotion.product_id == product.id))
    assert promo is not None
    assert promo.discount_percent == 10
    assert promo.duration_days == 7


async def test_merchant_flow_can_be_cancelled_mid_flow(db_session):
    business, product, contact, conversation = await _setup(db_session)

    await _merchant_says(db_session, business, contact, conversation, "ajouter un produit")
    reply = await _merchant_says(db_session, business, contact, conversation, "annuler")
    assert "annulé" in reply.text.lower()
    assert conversation.state["flow"] is None
    assert len(_row_titles(reply)) == 8  # merchant menu re-attached


async def test_sales_summary_single_turn(db_session):
    business, product, contact, conversation = await _setup(db_session)
    customer = Contact(business_id=business.id, wa_id="221770009999", phone_number="221770009999")
    db_session.add(customer)
    await db_session.flush()

    await create_order(
        db_session, business, customer, product, 1, "Client Test",
        DeliveryType.PICKUP, None, PaymentMethod.CASH,
    )

    reply = await _merchant_says(db_session, business, contact, conversation, "mes ventes")
    assert "Aujourd'hui" in reply.text
    assert "4500" in reply.text
    assert len(_row_titles(reply)) == 8  # menu re-attached after single-turn command


async def test_pending_orders_and_order_commands(db_session):
    business, product, contact, conversation = await _setup(db_session)
    customer = Contact(business_id=business.id, wa_id="221770009999", phone_number="221770009999")
    db_session.add(customer)
    await db_session.flush()

    order = await create_order(
        db_session, business, customer, product, 1, "Client Test",
        DeliveryType.PICKUP, None, PaymentMethod.CASH,
    )

    reply = await _merchant_says(db_session, business, contact, conversation, "mes commandes")
    assert order.order_number in reply.text

    reply = await _merchant_says(db_session, business, contact, conversation, f"{order.order_number} confirmer")
    assert "confirmée" in reply.text
    await db_session.flush()
    await db_session.refresh(order)
    assert order.status == OrderStatus.CONFIRMED

    reply = await _merchant_says(db_session, business, contact, conversation, f"{order.order_number} livrer")
    assert "livrée" in reply.text
    await db_session.flush()
    await db_session.refresh(order)
    assert order.status == OrderStatus.FULFILLED


async def test_catalog_view_groups_by_category_with_stock_numbers(db_session):
    business, product, contact, conversation = await _setup(db_session)  # Riz, no category, stock 10/None
    product.category = "Céréales"
    product.initial_stock = 20

    beauty_product = Product(
        business_id=business.id, name="Savon karité", category="Beauté", price_xof=1500,
        track_inventory=False,
    )
    db_session.add(beauty_product)
    await db_session.flush()

    reply = await _merchant_says(db_session, business, contact, conversation, "voir le catalogue")

    assert "*Céréales*" in reply.text
    assert "*Beauté*" in reply.text
    assert "Riz parfumé 5 kg" in reply.text
    assert "Stock actuel : 10 / initial : 20" in reply.text
    assert "Savon karité" in reply.text
    assert "Stock non suivi" in reply.text
    assert len(_row_titles(reply)) == 8  # menu re-attached


async def test_messages_en_attente_lists_flagged_conversations(db_session):
    business, product, contact, conversation = await _setup(db_session)
    customer = Contact(
        business_id=business.id, wa_id="221770009999", phone_number="221770009999", display_name="Client Perdu"
    )
    db_session.add(customer)
    await db_session.flush()

    flagged_conv = Conversation(
        business_id=business.id, contact_id=customer.id, state={"needs_human": True, "flow": None, "slots": {}}
    )
    db_session.add(flagged_conv)
    await db_session.flush()

    reply = await _merchant_says(db_session, business, contact, conversation, "messages en attente")
    assert "Client Perdu" in reply.text
    assert "221770009999" in reply.text
