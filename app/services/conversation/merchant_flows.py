"""Merchant admin, entirely via WhatsApp commands (the brief's core hypothesis
— no dashboard). Frequent one-off actions (checking sales, listing/updating
orders) are single-turn commands; actions that need several pieces of
information (adding a product, launching a promotion) are short FSM flows
using the same Conversation.state machinery as the customer order flow.

Every completed/cancelled flow and every single-turn command result is
wrapped in `with_merchant_menu()` so the merchant always sees the way back
to the main menu instead of having to remember what to type next."""

import re
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, Contact, Conversation, Order, OrderStatus, Product, Promotion
from app.services.conversation import state
from app.services.conversation.intents import Intent, detect_intent
from app.services.conversation.product_lookup import resolve_product
from app.services.conversation.reply import BotReply, with_cancel_button, with_merchant_menu
from app.services.orders.service import cancel_order, confirm_order, fulfill_order

ADD_PRODUCT_FLOW = "ajouter_produit"
EDIT_PRODUCT_FLOW = "modifier_produit"
DELETE_PRODUCT_FLOW = "supprimer_produit"
PROMOTION_FLOW = "lancer_promotion"

_ORDER_COMMAND_RE = re.compile(r"^(CMD-\d{8}-\d{4})\s+(confirmer|livrer|annuler)$", re.IGNORECASE)

_CONFIRM_BUTTONS = [("confirm", "Confirmer"), ("cancel", "Annuler")]
_YES_NO_BUTTONS = [("oui", "Oui"), ("non", "Non")]
_EDIT_FIELD_BUTTONS = [("prix", "Prix"), ("stock", "Stock"), ("disponibilite", "Disponibilité")]
_PROMO_DURATION_BUTTONS = [("3", "3 jours"), ("7", "7 jours"), ("14", "14 jours")]


async def _products(db: AsyncSession, business_id: UUID) -> list[Product]:
    return (
        await db.execute(
            select(Product).where(Product.business_id == business_id).order_by(Product.display_order, Product.name)
        )
    ).scalars().all()


def _product_list_sections(products: list[Product]) -> list[tuple[str, list[tuple[str, str, str]]]]:
    rows = [
        (
            str(p.id),
            p.name,
            f"{int(p.price_xof)} FCFA" + (" (indisponible)" if not p.is_available else ""),
        )
        for p in products
    ]
    return [("Produits", rows)]


async def _handle_order_command(db: AsyncSession, business: Business, text: str) -> str | None:
    match = _ORDER_COMMAND_RE.match(text.strip())
    if not match:
        return None
    order_number, action = match.group(1).upper(), match.group(2).lower()

    order = await db.scalar(
        select(Order).where(Order.business_id == business.id, Order.order_number == order_number)
    )
    if order is None:
        return f"Commande {order_number} introuvable."

    if action == "confirmer":
        if order.status != OrderStatus.PENDING:
            return f"{order_number} n'est pas en attente (statut actuel : {order.status.value})."
        await confirm_order(order)
        return f"{order_number} marquée comme confirmée."

    if action == "livrer":
        if order.status != OrderStatus.CONFIRMED:
            return f"{order_number} doit d'abord être confirmée (statut actuel : {order.status.value})."
        await fulfill_order(order)
        return f"{order_number} marquée comme livrée. Merci !"

    if order.status == OrderStatus.CANCELLED:
        return f"{order_number} est déjà annulée."
    await cancel_order(db, order, reason="Annulée par le marchand via WhatsApp")
    return f"{order_number} a été annulée."


async def _sales_summary(db: AsyncSession, business: Business) -> str:
    today = date.today()

    async def _count_and_revenue(where_today: bool) -> tuple[int, float]:
        stmt = select(func.count(), func.coalesce(func.sum(Order.total_xof), 0)).where(
            Order.business_id == business.id, Order.status != OrderStatus.CANCELLED
        )
        if where_today:
            stmt = stmt.where(func.date(Order.created_at) == today)
        row = (await db.execute(stmt)).one()
        return row[0], float(row[1])

    today_count, today_revenue = await _count_and_revenue(True)
    total_count, total_revenue = await _count_and_revenue(False)
    return (
        f"Aujourd'hui : {today_count} commande(s) pour {int(today_revenue)} FCFA.\n"
        f"Depuis le début : {total_count} commande(s) pour {int(total_revenue)} FCFA."
    )


async def _pending_orders_message(db: AsyncSession, business: Business) -> str:
    orders = (
        await db.execute(
            select(Order)
            .where(Order.business_id == business.id, Order.status.in_([OrderStatus.PENDING, OrderStatus.CONFIRMED]))
            .order_by(Order.created_at.asc())
        )
    ).scalars().all()
    if not orders:
        return "Aucune commande en attente."
    lines = [
        f"{o.order_number} — {o.customer_name or '?'} — {int(o.total_xof)} FCFA — {o.status.value}" for o in orders
    ]
    return (
        "Commandes en attente :\n" + "\n".join(lines) +
        "\n\nRépondez '<référence> confirmer' ou '<référence> livrer' pour mettre à jour une commande."
    )


async def _pending_human_handoffs(db: AsyncSession, business: Business) -> str:
    conversations = (
        await db.execute(
            select(Conversation)
            .where(Conversation.business_id == business.id)
            .order_by(Conversation.last_message_at.desc())
        )
    ).scalars().all()
    flagged = [c for c in conversations if state.needs_human(c)]
    if not flagged:
        return "Aucun message en attente d'une réponse humaine."

    lines = []
    for conv in flagged:
        contact = await db.get(Contact, conv.contact_id)
        if contact is None:
            continue
        name = contact.display_name or contact.phone_number
        lines.append(f"- {name} ({contact.phone_number}) : https://wa.me/{contact.phone_number}")
    return "Messages en attente d'une réponse humaine :\n" + "\n".join(lines)


async def handle_intent(db: AsyncSession, business: Business, conversation: Conversation, text: str) -> BotReply:
    order_command_reply = await _handle_order_command(db, business, text)
    if order_command_reply is not None:
        return with_merchant_menu(order_command_reply)

    intent = detect_intent(text, is_merchant=True)

    if intent == Intent.GREETING:
        return with_merchant_menu(f"Bonjour {business.owner_name or ''} ! Que souhaitez-vous faire ?".strip())

    if intent == Intent.GOODBYE:
        return BotReply(text="À bientôt ! -- Jaaykat bi")

    if intent == Intent.AJOUTER_PRODUIT:
        state.start_flow(conversation, ADD_PRODUCT_FLOW)
        return with_cancel_button("Quel est le nom du nouveau produit ?")

    if intent == Intent.MODIFIER_PRODUIT:
        products = await _products(db, business.id)
        if not products:
            return with_merchant_menu("Vous n'avez aucun produit à modifier.")
        state.start_flow(conversation, EDIT_PRODUCT_FLOW)
        return BotReply(
            text="Quel produit souhaitez-vous modifier ?",
            list_button_text="Choisir",
            list_sections=_product_list_sections(products),
        )

    if intent == Intent.SUPPRIMER_PRODUIT:
        products = await _products(db, business.id)
        if not products:
            return with_merchant_menu("Vous n'avez aucun produit à retirer.")
        state.start_flow(conversation, DELETE_PRODUCT_FLOW)
        return BotReply(
            text="Quel produit souhaitez-vous retirer du catalogue ?",
            list_button_text="Choisir",
            list_sections=_product_list_sections(products),
        )

    if intent == Intent.LANCER_PROMOTION:
        products = await _products(db, business.id)
        if not products:
            return with_merchant_menu("Vous n'avez aucun produit pour lancer une promotion.")
        state.start_flow(conversation, PROMOTION_FLOW)
        return BotReply(
            text="Quel produit souhaitez-vous mettre en promotion ?",
            list_button_text="Choisir",
            list_sections=_product_list_sections(products),
        )

    if intent == Intent.CONSULTER_VENTES:
        return with_merchant_menu(await _sales_summary(db, business))

    if intent == Intent.VOIR_COMMANDES:
        return with_merchant_menu(await _pending_orders_message(db, business))

    if intent == Intent.MESSAGES_EN_ATTENTE:
        return with_merchant_menu(await _pending_human_handoffs(db, business))

    return with_merchant_menu("Je n'ai pas compris. Que souhaitez-vous faire ?")


async def continue_flow(db: AsyncSession, business: Business, conversation: Conversation, text: str) -> BotReply:
    flow = state.current_flow(conversation)
    if flow == ADD_PRODUCT_FLOW:
        return await _continue_add_product(db, business, conversation, text)
    if flow == EDIT_PRODUCT_FLOW:
        return await _continue_edit_product(db, business, conversation, text)
    if flow == DELETE_PRODUCT_FLOW:
        return await _continue_delete_product(db, conversation, text)
    if flow == PROMOTION_FLOW:
        return await _continue_promotion(db, business, conversation, text)

    state.clear_flow(conversation)
    return with_merchant_menu("Que souhaitez-vous faire ?")


async def _continue_add_product(db: AsyncSession, business: Business, conversation: Conversation, text: str) -> BotReply:
    step = state.current_step(conversation)
    slots = state.current_slots(conversation)

    if step == 0:
        name = text.strip()
        if not name:
            return with_cancel_button("Merci d'indiquer un nom de produit.")
        slots["name"] = name
        state.advance(conversation, 1, slots)
        return with_cancel_button("Quelle catégorie ? (ou répondez '-' si aucune)")

    if step == 1:
        category = text.strip()
        slots["category"] = None if category == "-" else category
        state.advance(conversation, 2, slots)
        return with_cancel_button("Quel est le prix en FCFA ?")

    if step == 2:
        try:
            price = float(re.sub(r"[^\d.]", "", text))
            if price <= 0:
                raise ValueError
        except ValueError:
            return with_cancel_button("Merci d'indiquer un prix valide (ex : 4500).")
        slots["price"] = price
        state.advance(conversation, 3, slots)
        return with_cancel_button("Quel est le stock initial ? (un nombre, ou 'illimité' si vous ne suivez pas le stock)")

    if step == 3:
        t = text.strip().lower()
        if t in {"illimité", "illimite", "non"}:
            slots["track_inventory"] = False
            slots["stock"] = None
        else:
            match = re.search(r"\d+", text)
            if not match:
                return with_cancel_button("Merci d'indiquer un nombre, ou 'illimité'.")
            slots["track_inventory"] = True
            slots["stock"] = int(match.group())
        state.advance(conversation, 4, slots)
        stock_line = "suivi non activé" if not slots["track_inventory"] else f"{slots['stock']} unité(s)"
        recap = f"Récapitulatif : {slots['name']} — {int(slots['price'])} FCFA — {stock_line}."
        return BotReply(text=recap, buttons=_CONFIRM_BUTTONS)

    if step == 4:
        if "confirmer" not in text.lower():
            return BotReply(text="Voulez-vous ajouter ce produit ?", buttons=_CONFIRM_BUTTONS)
        product = Product(
            business_id=business.id,
            name=slots["name"],
            category=slots.get("category"),
            price_xof=slots["price"],
            track_inventory=slots["track_inventory"],
            quantity_in_stock=slots["stock"],
        )
        db.add(product)
        state.clear_flow(conversation)
        return with_merchant_menu(f"{product.name} a été ajouté au catalogue. -- Jaaykat bi")

    state.clear_flow(conversation)
    return with_merchant_menu("Que souhaitez-vous faire ?")


async def _continue_edit_product(db: AsyncSession, business: Business, conversation: Conversation, text: str) -> BotReply:
    step = state.current_step(conversation)
    slots = state.current_slots(conversation)
    products = await _products(db, business.id)

    if step == 0:
        product = resolve_product(text, products)
        if product is None:
            return BotReply(
                text="Produit introuvable. Choisissez dans la liste :",
                list_button_text="Choisir",
                list_sections=_product_list_sections(products),
            )
        slots["product_id"] = str(product.id)
        state.advance(conversation, 1, slots)
        return BotReply(text=f"Que souhaitez-vous modifier pour {product.name} ?", buttons=_EDIT_FIELD_BUTTONS)

    if step == 1:
        t = text.strip().lower()
        if t.startswith("1") or "prix" in t:
            field = "price"
        elif t.startswith("2") or "stock" in t:
            field = "stock"
        elif t.startswith("3") or "disponib" in t:
            field = "availability"
        else:
            return BotReply(text="Que souhaitez-vous modifier ?", buttons=_EDIT_FIELD_BUTTONS)
        slots["field"] = field
        state.advance(conversation, 2, slots)
        if field == "availability":
            return BotReply(text="Le produit doit-il être disponible ?", buttons=_YES_NO_BUTTONS)
        prompts = {"price": "Quel est le nouveau prix en FCFA ?", "stock": "Quel est le nouveau stock ?"}
        return with_cancel_button(prompts[field])

    if step == 2:
        product = await db.get(Product, UUID(slots["product_id"]))
        field = slots["field"]
        if field == "price":
            try:
                price = float(re.sub(r"[^\d.]", "", text))
                if price <= 0:
                    raise ValueError
            except ValueError:
                return with_cancel_button("Merci d'indiquer un prix valide (ex : 4500).")
            product.price_xof = price
            confirmation = f"Le prix de {product.name} est désormais {int(price)} FCFA."
        elif field == "stock":
            match = re.search(r"\d+", text)
            if not match:
                return with_cancel_button("Merci d'indiquer un nombre.")
            product.track_inventory = True
            product.quantity_in_stock = int(match.group())
            confirmation = f"Le stock de {product.name} est désormais {product.quantity_in_stock}."
        else:
            t = text.strip().lower()
            if t not in {"oui", "non"}:
                return BotReply(text="Le produit doit-il être disponible ?", buttons=_YES_NO_BUTTONS)
            product.is_available = t == "oui"
            confirmation = f"{product.name} est désormais {'disponible' if product.is_available else 'indisponible'}."
        state.clear_flow(conversation)
        return with_merchant_menu(f"{confirmation} -- Jaaykat bi")

    state.clear_flow(conversation)
    return with_merchant_menu("Que souhaitez-vous faire ?")


async def _continue_delete_product(db: AsyncSession, conversation: Conversation, text: str) -> BotReply:
    step = state.current_step(conversation)
    slots = state.current_slots(conversation)

    if step == 0:
        business_products = await _products_for_conversation(db, conversation)
        product = resolve_product(text, business_products)
        if product is None:
            return BotReply(
                text="Produit introuvable. Choisissez dans la liste :",
                list_button_text="Choisir",
                list_sections=_product_list_sections(business_products),
            )
        slots["product_id"] = str(product.id)
        state.advance(conversation, 1, slots)
        return BotReply(text=f"Confirmez-vous le retrait de {product.name} du catalogue ?", buttons=_YES_NO_BUTTONS)

    if step == 1:
        t = text.strip().lower()
        if t not in {"oui", "non"}:
            return BotReply(text="Confirmez-vous le retrait de ce produit ?", buttons=_YES_NO_BUTTONS)
        product = await db.get(Product, UUID(slots["product_id"]))
        if t == "oui":
            product.is_available = False
            state.clear_flow(conversation)
            return with_merchant_menu(f"{product.name} a été retiré du catalogue. -- Jaaykat bi")
        state.clear_flow(conversation)
        return with_merchant_menu("D'accord, le produit reste dans le catalogue.")

    state.clear_flow(conversation)
    return with_merchant_menu("Que souhaitez-vous faire ?")


async def _products_for_conversation(db: AsyncSession, conversation: Conversation) -> list[Product]:
    business = await db.get(Business, conversation.business_id)
    return await _products(db, business.id)


async def _continue_promotion(db: AsyncSession, business: Business, conversation: Conversation, text: str) -> BotReply:
    step = state.current_step(conversation)
    slots = state.current_slots(conversation)
    products = await _products(db, business.id)

    if step == 0:
        product = resolve_product(text, products)
        if product is None:
            return BotReply(
                text="Produit introuvable. Choisissez dans la liste :",
                list_button_text="Choisir",
                list_sections=_product_list_sections(products),
            )
        slots["product_id"] = str(product.id)
        state.advance(conversation, 1, slots)
        return with_cancel_button("Quel pourcentage de réduction ? (ex : 10)")

    if step == 1:
        match = re.search(r"\d+", text)
        if not match or not (0 < int(match.group()) < 100):
            return with_cancel_button("Merci d'indiquer un pourcentage valide entre 1 et 99.")
        slots["discount_percent"] = int(match.group())
        state.advance(conversation, 2, slots)
        return BotReply(text="Pendant combien de jours ?", buttons=_PROMO_DURATION_BUTTONS)

    if step == 2:
        match = re.search(r"\d+", text)
        if not match or int(match.group()) <= 0:
            return BotReply(text="Pendant combien de jours ?", buttons=_PROMO_DURATION_BUTTONS)
        slots["duration_days"] = int(match.group())
        state.advance(conversation, 3, slots)
        product = await db.get(Product, UUID(slots["product_id"]))
        discounted = round(float(product.price_xof) * (1 - slots["discount_percent"] / 100), 2)
        recap = (
            f"Promotion : {product.name} à {int(discounted)} FCFA (au lieu de {int(product.price_xof)} FCFA) "
            f"pendant {slots['duration_days']} jours."
        )
        return BotReply(text=recap, buttons=_CONFIRM_BUTTONS)

    if step == 3:
        if "confirmer" not in text.lower():
            return BotReply(text="Voulez-vous activer cette promotion ?", buttons=_CONFIRM_BUTTONS)
        product = await db.get(Product, UUID(slots["product_id"]))
        promo = Promotion(
            business_id=business.id,
            product_id=product.id,
            title=f"Promo {slots['discount_percent']}%",
            discount_percent=slots["discount_percent"],
            duration_days=slots["duration_days"],
            end_date=datetime.now(UTC) + timedelta(days=slots["duration_days"]),
        )
        db.add(promo)
        state.clear_flow(conversation)
        return with_merchant_menu(f"Promotion activée sur {product.name}. -- Jaaykat bi")

    state.clear_flow(conversation)
    return with_merchant_menu("Que souhaitez-vous faire ?")
