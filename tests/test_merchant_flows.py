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
from app.services.conversation import engine, state
from app.services.conversation.engine import handle_message
from app.services.orders.service import confirm_order, create_order

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
    assert len(_row_titles(reply)) == 9  # merchant menu re-attached

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
    assert set(_row_titles(reply)) == {"Nom", "Prix", "Stock", "Disponibilité", "Annuler"}

    reply = await _merchant_says(db_session, business, contact, conversation, "Prix")
    assert "nouveau prix" in reply.text.lower()

    reply = await _merchant_says(db_session, business, contact, conversation, "5000")
    assert "désormais 5000" in reply.text

    await db_session.flush()
    await db_session.refresh(product)
    assert float(product.price_xof) == 5000.0
    assert conversation.state["flow"] is None


async def test_edit_product_name_field(db_session):
    business, product, contact, conversation = await _setup(db_session)

    await _merchant_says(db_session, business, contact, conversation, "modifier un produit")
    reply = await _merchant_says(db_session, business, contact, conversation, "1")
    assert "Nom" in _row_titles(reply)

    reply = await _merchant_says(db_session, business, contact, conversation, "Nom")
    assert "nouveau nom" in reply.text.lower()

    reply = await _merchant_says(db_session, business, contact, conversation, "Riz parfumé 10 kg")
    assert "renommé" in reply.text

    await db_session.flush()
    await db_session.refresh(product)
    assert product.name == "Riz parfumé 10 kg"
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


async def test_launch_promotion_single_product_mode(db_session):
    business, product, contact, conversation = await _setup(db_session)

    reply = await _merchant_says(db_session, business, contact, conversation, "lancer une promotion")
    assert set(_row_titles(reply)) == {"Un produit", "Toute une catégorie", "Plusieurs produits", "Annuler"}

    reply = await _merchant_says(db_session, business, contact, conversation, "Un produit")
    assert "Annuler" in _row_titles(reply)

    await _merchant_says(db_session, business, contact, conversation, "1")
    reply = await _merchant_says(db_session, business, contact, conversation, "10")
    assert set(_row_titles(reply)) == {"3 jours", "7 jours", "14 jours", "Annuler"}

    reply = await _merchant_says(db_session, business, contact, conversation, "7")
    assert set(_button_titles(reply)) == {"Confirmer", "Annuler"}
    assert product.name in reply.text

    reply = await _merchant_says(db_session, business, contact, conversation, "confirmer")
    assert "Promotion activée sur 1 produit" in reply.text

    promo = await db_session.scalar(select(Promotion).where(Promotion.product_id == product.id))
    assert promo is not None
    assert promo.discount_percent == 10
    assert promo.duration_days == 7


async def test_launch_promotion_category_mode_applies_to_every_product_in_category(db_session):
    business, product, contact, conversation = await _setup(db_session)
    product.category = "Céréales"
    other_product = Product(
        business_id=business.id, name="Sucre en poudre", category="Céréales", price_xof=800,
    )
    other_category_product = Product(
        business_id=business.id, name="Savon karité", category="Beauté", price_xof=1500,
    )
    db_session.add_all([other_product, other_category_product])
    await db_session.flush()

    await _merchant_says(db_session, business, contact, conversation, "lancer une promotion")
    reply = await _merchant_says(db_session, business, contact, conversation, "Toute une catégorie")
    assert set(_row_titles(reply)) == {"Céréales", "Beauté", "Annuler"}

    await _merchant_says(db_session, business, contact, conversation, "Céréales")
    reply = await _merchant_says(db_session, business, contact, conversation, "20")
    await _merchant_says(db_session, business, contact, conversation, "3")
    reply = await _merchant_says(db_session, business, contact, conversation, "confirmer")
    assert "Promotion activée sur 2 produits" in reply.text

    promos = (await db_session.execute(select(Promotion).where(Promotion.business_id == business.id))).scalars().all()
    promo_product_ids = {p.product_id for p in promos}
    assert promo_product_ids == {product.id, other_product.id}  # not the "Beauté" one
    assert all(p.discount_percent == 20 and p.duration_days == 3 for p in promos)


async def test_launch_promotion_multi_select_mode_across_categories(db_session):
    business, product, contact, conversation = await _setup(db_session)  # "Riz parfumé 5 kg", no category
    beauty_product = Product(
        business_id=business.id, name="Savon karité", category="Beauté", price_xof=1500,
    )
    db_session.add(beauty_product)
    await db_session.flush()

    await _merchant_says(db_session, business, contact, conversation, "lancer une promotion")
    reply = await _merchant_says(db_session, business, contact, conversation, "Plusieurs produits")
    assert product.name in _row_titles(reply)
    assert beauty_product.name in _row_titles(reply)

    reply = await _merchant_says(db_session, business, contact, conversation, product.name)
    assert set(_button_titles(reply)) == {"Ajouter un autre", "Terminé", "Annuler"}
    assert "1 sélectionné" in reply.text

    reply = await _merchant_says(db_session, business, contact, conversation, "Ajouter un autre")
    assert beauty_product.name in _row_titles(reply)
    assert product.name not in _row_titles(reply)  # already picked, excluded from the remaining list

    await _merchant_says(db_session, business, contact, conversation, beauty_product.name)
    reply = await _merchant_says(db_session, business, contact, conversation, "Terminé")
    assert "réduction" in reply.text.lower()

    await _merchant_says(db_session, business, contact, conversation, "15")
    reply = await _merchant_says(db_session, business, contact, conversation, "14")
    assert "2 produit" in reply.text
    reply = await _merchant_says(db_session, business, contact, conversation, "confirmer")
    assert "Promotion activée sur 2 produits" in reply.text

    promos = (await db_session.execute(select(Promotion).where(Promotion.business_id == business.id))).scalars().all()
    assert {p.product_id for p in promos} == {product.id, beauty_product.id}


async def test_catalog_view_shows_promo_tag_and_updated_price(db_session):
    business, product, contact, conversation = await _setup(db_session)
    db_session.add(
        Promotion(
            business_id=business.id, product_id=product.id, title="Promo -10%",
            discount_percent=10, duration_days=7, end_date=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await db_session.flush()

    reply = await _merchant_says(db_session, business, contact, conversation, "voir le catalogue")
    assert "🏷️ PROMO -10%" in reply.text
    assert "~4500~" in reply.text
    assert "4050 FCFA" in reply.text

    promo = await db_session.scalar(select(Promotion).where(Promotion.product_id == product.id))
    assert promo is not None
    assert promo.discount_percent == 10
    assert promo.duration_days == 7


async def test_stop_promotion_flow_ends_it_before_its_due_date(db_session):
    business, product, contact, conversation = await _setup(db_session)
    db_session.add(
        Promotion(
            business_id=business.id, product_id=product.id, title="Promo -10%",
            discount_percent=10, duration_days=7, end_date=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await db_session.flush()

    reply = await _merchant_says(db_session, business, contact, conversation, "Arrêter une promotion")
    assert product.name in _row_titles(reply)

    reply = await _merchant_says(db_session, business, contact, conversation, product.name)
    assert set(_button_titles(reply)) == {"Oui", "Non", "Annuler"}
    assert product.name in reply.text

    reply = await _merchant_says(db_session, business, contact, conversation, "oui")
    assert "arrêtée" in reply.text.lower()
    assert conversation.state["flow"] is None

    await db_session.flush()
    promo = await db_session.scalar(select(Promotion).where(Promotion.product_id == product.id))
    assert promo.is_active is False

    # catalog view no longer shows the promo tag for this product
    reply = await _merchant_says(db_session, business, contact, conversation, "voir le catalogue")
    assert "PROMO" not in reply.text
    assert "4500 FCFA" in reply.text


async def test_stop_promotion_flow_reports_none_active(db_session):
    business, product, contact, conversation = await _setup(db_session)

    reply = await _merchant_says(db_session, business, contact, conversation, "Arrêter une promotion")
    assert "Aucune promotion en cours" in reply.text
    assert conversation.state.get("flow") is None  # no flow started — nothing to pick from


async def test_merchant_flow_can_be_cancelled_mid_flow(db_session):
    business, product, contact, conversation = await _setup(db_session)

    await _merchant_says(db_session, business, contact, conversation, "ajouter un produit")
    reply = await _merchant_says(db_session, business, contact, conversation, "annuler")
    assert "annulé" in reply.text.lower()
    assert conversation.state["flow"] is None
    assert len(_row_titles(reply)) == 9  # merchant menu re-attached


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
    assert len(_row_titles(reply)) == 9  # menu re-attached after single-turn command


async def test_pending_orders_typed_shortcut_still_works(db_session):
    business, product, contact, conversation = await _setup(db_session)
    customer = Contact(business_id=business.id, wa_id="221770009999", phone_number="221770009999")
    db_session.add(customer)
    await db_session.flush()

    order = await create_order(
        db_session, business, customer, product, 1, "Client Test",
        DeliveryType.PICKUP, None, PaymentMethod.CASH,
    )

    reply = await _merchant_says(db_session, business, contact, conversation, "mes commandes")
    assert order.order_number in _row_titles(reply)

    # typed "<ref> action" shorthand works even with the order-list flow active
    reply = await _merchant_says(db_session, business, contact, conversation, f"{order.order_number} confirmer")
    assert "confirmée" in reply.text
    assert conversation.state.get("flow") is None  # shortcut resolves and exits the flow
    await db_session.flush()
    await db_session.refresh(order)
    assert order.status == OrderStatus.CONFIRMED

    reply = await _merchant_says(db_session, business, contact, conversation, f"{order.order_number} livrer")
    assert "livrée" in reply.text
    await db_session.flush()
    await db_session.refresh(order)
    assert order.status == OrderStatus.FULFILLED


async def test_pending_orders_guided_list_and_submenu_confirm(db_session):
    business, product, contact, conversation = await _setup(db_session)
    customer = Contact(business_id=business.id, wa_id="221770009999", phone_number="221770009999")
    db_session.add(customer)
    await db_session.flush()

    order = await create_order(
        db_session, business, customer, product, 1, "Client Test",
        DeliveryType.PICKUP, None, PaymentMethod.CASH,
    )

    reply = await _merchant_says(db_session, business, contact, conversation, "mes commandes")
    assert order.order_number in _row_titles(reply)
    assert "Annuler" in _row_titles(reply)  # universal escape row, distinct from "Annuler la commande"

    reply = await _merchant_says(db_session, business, contact, conversation, order.order_number)
    assert set(_button_titles(reply)) == {"Confirmer", "Annuler la commande", "Retour"}
    assert order.order_number in reply.text

    reply = await _merchant_says(db_session, business, contact, conversation, "Confirmer")
    assert "confirmée" in reply.text
    assert conversation.state.get("flow") is None
    await db_session.flush()
    await db_session.refresh(order)
    assert order.status == OrderStatus.CONFIRMED


async def test_pending_orders_submenu_fulfill_action_via_button_tap(db_session):
    # Regression test: the submenu button is titled "Marquer livrée" (past
    # participle) — a match on the substring "livrer" (infinitive) never
    # fires for that title, so the tap silently did nothing. Only the typed
    # "<ref> livrer" shortcut exercised the "livrer" substring, which is why
    # this slipped through before.
    business, product, contact, conversation = await _setup(db_session)
    customer = Contact(business_id=business.id, wa_id="221770009999", phone_number="221770009999")
    db_session.add(customer)
    await db_session.flush()

    order = await create_order(
        db_session, business, customer, product, 1, "Client Test",
        DeliveryType.PICKUP, None, PaymentMethod.CASH,
    )
    await confirm_order(order)
    await db_session.flush()

    await _merchant_says(db_session, business, contact, conversation, "mes commandes")
    reply = await _merchant_says(db_session, business, contact, conversation, order.order_number)
    assert set(_button_titles(reply)) == {"Marquer livrée", "Annuler la commande", "Retour"}

    reply = await _merchant_says(db_session, business, contact, conversation, "Marquer livrée")
    assert "livrée" in reply.text
    assert conversation.state.get("flow") is None

    await db_session.flush()
    await db_session.refresh(order)
    assert order.status == OrderStatus.FULFILLED
    assert order.fulfilled_at is not None


async def test_pending_orders_submenu_cancel_order_action(db_session):
    business, product, contact, conversation = await _setup(db_session)
    customer = Contact(business_id=business.id, wa_id="221770009999", phone_number="221770009999")
    db_session.add(customer)
    await db_session.flush()

    order = await create_order(
        db_session, business, customer, product, 1, "Client Test",
        DeliveryType.PICKUP, None, PaymentMethod.CASH,
    )

    await _merchant_says(db_session, business, contact, conversation, "mes commandes")
    await _merchant_says(db_session, business, contact, conversation, order.order_number)
    reply = await _merchant_says(db_session, business, contact, conversation, "Annuler la commande")
    assert "a été annulée" in reply.text

    await db_session.flush()
    await db_session.refresh(order)
    assert order.status == OrderStatus.CANCELLED
    await db_session.refresh(product)
    assert product.quantity_in_stock == 10  # stock restored


async def test_pending_orders_retour_goes_back_to_the_list(db_session):
    business, product, contact, conversation = await _setup(db_session)
    customer = Contact(business_id=business.id, wa_id="221770009999", phone_number="221770009999")
    db_session.add(customer)
    await db_session.flush()

    order = await create_order(
        db_session, business, customer, product, 1, "Client Test",
        DeliveryType.PICKUP, None, PaymentMethod.CASH,
    )

    await _merchant_says(db_session, business, contact, conversation, "mes commandes")
    await _merchant_says(db_session, business, contact, conversation, order.order_number)
    reply = await _merchant_says(db_session, business, contact, conversation, "Retour")

    assert order.order_number in _row_titles(reply)
    assert conversation.state["step"] == 0  # back at the list, flow still active
    assert conversation.state["flow"] == "voir_commandes"


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
    assert len(_row_titles(reply)) == 9  # menu re-attached


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


async def test_merchant_unmatched_question_falls_back_to_llm(db_session, monkeypatch):
    business, product, contact, conversation = await _setup(db_session)

    captured_prompt = {}

    async def fake_generate(system_prompt, history, text):
        captured_prompt["value"] = system_prompt
        return "Votre produit le plus cher est Riz parfumé 5 kg à 4500 FCFA."

    monkeypatch.setattr(engine._llm_client, "generate", fake_generate)

    reply = await _merchant_says(
        db_session, business, contact, conversation, "Quel est mon produit le plus cher ?"
    )
    assert "4500 FCFA" in reply.text
    assert len(_row_titles(reply)) == 9  # menu re-attached, same as every other merchant reply
    assert "Riz parfumé 5 kg" in captured_prompt["value"]  # catalog context reached the LLM
    assert conversation.state.get("flow") is None  # this is a plain Q&A, not a flow


async def test_merchant_llm_fallback_failure_gives_a_plain_retry_message(db_session, monkeypatch):
    business, product, contact, conversation = await _setup(db_session)

    async def fake_generate_failure(system_prompt, history, text):
        return None

    monkeypatch.setattr(engine._llm_client, "generate", fake_generate_failure)

    reply = await _merchant_says(db_session, business, contact, conversation, "Quel est mon produit le plus cher ?")
    assert "reformuler" in reply.text.lower()
    assert len(_row_titles(reply)) == 9
