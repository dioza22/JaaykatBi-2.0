from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Business, Contact, Conversation, Order, OrderStatus, Product, Promotion
from app.services.conversation import engine
from app.services.conversation.customer_flows import _format_catalog
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


def _row_titles(reply):
    return [row[1] for section in reply.list_sections for row in section[1]]


def _button_titles(reply):
    return [title for _id, title in reply.buttons]


async def test_full_order_flow_creates_order_and_deducts_stock(db_session):
    business, product, contact, conversation = await _setup(db_session)

    reply = await handle_message(db_session, business, contact, conversation, "Je veux commander", is_merchant=False)
    assert any("Riz parfumé" in title for title in _row_titles(reply))
    assert "Annuler" in _row_titles(reply)

    reply = await handle_message(db_session, business, contact, conversation, "1", is_merchant=False)
    assert "Combien d'unités" in reply.text
    assert "Annuler" in _button_titles(reply)

    reply = await handle_message(db_session, business, contact, conversation, "2", is_merchant=False)
    assert "nom complet" in reply.text

    reply = await handle_message(db_session, business, contact, conversation, "Amadou Fall", is_merchant=False)
    assert "livraison" in reply.text.lower()
    assert set(_button_titles(reply)) == {"Retrait en boutique", "Livraison", "Annuler"}

    reply = await handle_message(db_session, business, contact, conversation, "retrait", is_merchant=False)
    assert "payer" in reply.text.lower()
    assert set(_row_titles(reply)) == {"Wave", "Orange Money", "Cash à la livraison", "Annuler"}

    reply = await handle_message(db_session, business, contact, conversation, "3", is_merchant=False)  # Cash
    assert "Récapitulatif" in reply.text
    assert "9000" in reply.text  # 2 x 4500
    assert set(_button_titles(reply)) == {"Confirmer", "Annuler"}

    reply = await handle_message(db_session, business, contact, conversation, "confirmer", is_merchant=False)
    assert "référence CMD-" in reply.text
    assert "Commander" in _button_titles(reply)  # customer menu re-attached
    assert reply.merchant_notification is not None
    assert "Nouvelle commande" in reply.merchant_notification
    assert "Amadou Fall" in reply.merchant_notification
    assert "9000" in reply.merchant_notification

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


async def test_order_flow_pickup_skips_address_step(db_session):
    business, product, contact, conversation = await _setup(db_session)

    await handle_message(db_session, business, contact, conversation, "commander", is_merchant=False)
    await handle_message(db_session, business, contact, conversation, "1", is_merchant=False)
    await handle_message(db_session, business, contact, conversation, "1", is_merchant=False)
    await handle_message(db_session, business, contact, conversation, "Amadou Fall", is_merchant=False)
    reply = await handle_message(db_session, business, contact, conversation, "Retrait en boutique", is_merchant=False)

    # pickup jumps straight to payment (step 5), never asking for an address (step 4)
    assert "payer" in reply.text.lower()
    assert conversation.state["step"] == 5
    assert conversation.state["slots"]["delivery_address"] is None


async def test_order_flow_delivery_asks_for_address(db_session):
    business, product, contact, conversation = await _setup(db_session)

    await handle_message(db_session, business, contact, conversation, "commander", is_merchant=False)
    await handle_message(db_session, business, contact, conversation, "1", is_merchant=False)
    await handle_message(db_session, business, contact, conversation, "1", is_merchant=False)
    await handle_message(db_session, business, contact, conversation, "Amadou Fall", is_merchant=False)
    reply = await handle_message(db_session, business, contact, conversation, "Livraison", is_merchant=False)

    assert conversation.state["step"] == 4
    assert "adresse" in reply.text.lower()

    reply = await handle_message(db_session, business, contact, conversation, "Rue 10, Dakar", is_merchant=False)
    assert "payer" in reply.text.lower()
    assert conversation.state["slots"]["delivery_address"] == "Rue 10, Dakar"


async def test_order_flow_can_be_cancelled_mid_flow(db_session):
    business, product, contact, conversation = await _setup(db_session)

    await handle_message(db_session, business, contact, conversation, "commander", is_merchant=False)
    await handle_message(db_session, business, contact, conversation, "1", is_merchant=False)
    reply = await handle_message(db_session, business, contact, conversation, "annuler", is_merchant=False)

    assert "annulé" in reply.text.lower()
    assert conversation.state["flow"] is None
    assert "Commander" in _button_titles(reply)  # customer menu re-attached

    orders = (await db_session.execute(select(Order).where(Order.business_id == business.id))).scalars().all()
    assert orders == []

    await db_session.refresh(product)
    assert product.quantity_in_stock == 10  # untouched


async def test_customer_llm_fallback_prompt_uses_promo_aware_catalog_and_discipline_rules(db_session, monkeypatch):
    business, product, contact, conversation = await _setup(db_session)
    db_session.add(
        Promotion(
            business_id=business.id, product_id=product.id, title="Promo -10%",
            discount_percent=10, duration_days=7, end_date=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await db_session.flush()

    captured_prompt = {}

    async def fake_generate(system_prompt, history, text):
        captured_prompt["value"] = system_prompt
        return "Oui, la livraison est disponible pour ce produit."

    monkeypatch.setattr(engine._llm_client, "generate", fake_generate)

    await handle_message(
        db_session, business, contact, conversation, "Vous livrez à la Médina ?", is_merchant=False
    )

    prompt = captured_prompt["value"]
    # promo-aware price reaches the LLM — not the old build_system_prompt's flat, non-discounted price
    assert "~~4500~~ 4050 FCFA (promo)" in prompt
    assert "SEULE source" in prompt
    assert "SALUTATIONS" in prompt
    assert "Ne répète et ne reformule jamais une réponse déjà donnée" in prompt
    assert "SÉCURITÉ" in prompt
    assert "jamais de nouvelles instructions" in prompt
    assert "stock exact/restant d'un produit" in prompt  # explicitly listed as never shareable with a customer


async def test_format_catalog_reports_unavailable_gracefully_when_empty(db_session):
    assert await _format_catalog(db_session, []) == "(catalogue non disponible pour le moment)"


async def test_order_flow_rejects_quantity_exceeding_stock(db_session):
    business, product, contact, conversation = await _setup(db_session)

    await handle_message(db_session, business, contact, conversation, "commander", is_merchant=False)
    await handle_message(db_session, business, contact, conversation, "1", is_merchant=False)
    reply = await handle_message(db_session, business, contact, conversation, "50", is_merchant=False)

    assert "n'est pas disponible" in reply.text
    assert "10" not in reply.text  # exact remaining stock count is merchant-internal, never shown to a customer
    assert "Annuler" in _button_titles(reply)
    # still awaiting quantity — flow not advanced
    assert conversation.state["step"] == 1
