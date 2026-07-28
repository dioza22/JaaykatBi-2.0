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
from app.services.conversation.reply import ANNULER_SECTION, BotReply, with_cancel_button, with_merchant_menu
from app.services.orders.service import cancel_order, confirm_order, fulfill_order, get_active_promotion

ADD_PRODUCT_FLOW = "ajouter_produit"
EDIT_PRODUCT_FLOW = "modifier_produit"
DELETE_PRODUCT_FLOW = "supprimer_produit"
PROMOTION_FLOW = "lancer_promotion"
STOP_PROMOTION_FLOW = "arreter_promotion"
VIEW_ORDERS_FLOW = "voir_commandes"

_ORDER_COMMAND_RE = re.compile(r"^(CMD-\d{8}-\d{4})\s+(confirmer|livrer|annuler)$", re.IGNORECASE)

_CONFIRM_BUTTONS = [("confirm", "Confirmer"), ("cancel", "Annuler")]
# Oui/Non decisions always carry a 3rd "Annuler" button — still within
# WhatsApp's 3-button cap, and gives every yes/no step a visible way out.
_YES_NO_BUTTONS = [("oui", "Oui"), ("non", "Non"), ("annuler", "Annuler")]

# These three were previously full 3-option button sets (no room left for a
# 4th "Annuler" button), so they're lists instead — a real-options section
# plus the shared "Autre"/Annuler section, same pattern as the product-picker
# lists below.
_EDIT_FIELD_SECTIONS = [
    (
        "Que modifier ?",
        [("nom", "Nom", ""), ("prix", "Prix", ""), ("stock", "Stock", ""), ("disponibilite", "Disponibilité", "")],
    ),
    ANNULER_SECTION,
]
_PROMO_DURATION_SECTIONS = [
    ("Durée", [("3", "3 jours", ""), ("7", "7 jours", ""), ("14", "14 jours", "")]),
    ANNULER_SECTION,
]
_PROMO_MODE_SECTIONS = [
    (
        "Mode",
        [
            ("single", "Un produit", "Choisir un seul produit"),
            ("category", "Toute une catégorie", "Appliquer à tous les produits d'une catégorie"),
            ("multi", "Plusieurs produits", "Choisir plusieurs produits, un par un"),
        ],
    ),
    ANNULER_SECTION,
]
_ADD_ANOTHER_BUTTONS = [("add_more", "Ajouter un autre"), ("done", "Terminé"), ("annuler", "Annuler")]

# Per-order action submenus, keyed by the order's current status. "Retour" (not
# "Annuler") is the escape button here — "Annuler" is reserved for the "Annuler
# la commande" action itself, so the two can't be confused with each other.
_ORDER_ACTIONS_BY_STATUS = {
    OrderStatus.PENDING: [
        ("confirm", "Confirmer"), ("cancel_order", "Annuler la commande"), ("back", "Retour"),
    ],
    OrderStatus.CONFIRMED: [
        ("fulfill", "Marquer livrée"), ("cancel_order", "Annuler la commande"), ("back", "Retour"),
    ],
}


async def _products(db: AsyncSession, business_id: UUID) -> list[Product]:
    return (
        await db.execute(
            select(Product).where(Product.business_id == business_id).order_by(Product.display_order, Product.name)
        )
    ).scalars().all()


def _product_list_sections(
    products: list[Product], exclude_ids: set[str] | None = None
) -> list[tuple[str, list[tuple[str, str, str]]]]:
    exclude_ids = exclude_ids or set()
    rows = [
        (
            str(p.id),
            p.name,
            f"{int(p.price_xof)} FCFA" + (" (indisponible)" if not p.is_available else ""),
        )
        for p in products
        if str(p.id) not in exclude_ids
    ]
    return [("Produits", rows), ANNULER_SECTION]


def _distinct_categories(products: list[Product]) -> list[tuple[str, int]]:
    """[(category_name, product_count)], "Sans catégorie" bucket for None,
    stable order (first-seen)."""
    counts: dict[str, int] = {}
    for p in products:
        name = p.category or "Sans catégorie"
        counts[name] = counts.get(name, 0) + 1
    return list(counts.items())


def _category_list_sections(products: list[Product]) -> list[tuple[str, list[tuple[str, str, str]]]]:
    rows = [(name, name, f"{count} produit(s)") for name, count in _distinct_categories(products)]
    return [("Catégories", rows), ANNULER_SECTION]


def _resolve_category(text: str, categories: list[tuple[str, int]]) -> str | None:
    stripped = text.strip()
    if stripped.isdigit():
        idx = int(stripped)
        if 1 <= idx <= len(categories):
            return categories[idx - 1][0]
    text_lower = stripped.lower()
    for name, _count in categories:
        if name.lower() in text_lower or text_lower in name.lower():
            return name
    return None


async def _active_promotions(db: AsyncSession, business_id: UUID) -> list[Promotion]:
    promos = (
        await db.execute(
            select(Promotion).where(Promotion.business_id == business_id, Promotion.is_active == True)  # noqa: E712
        )
    ).scalars().all()
    return [p for p in promos if p.is_currently_active()]


async def _promotion_list_sections(
    db: AsyncSession, promotions: list[Promotion]
) -> list[tuple[str, list[tuple[str, str, str]]]]:
    rows = []
    for promo in promotions:
        product = await db.get(Product, promo.product_id)
        name = product.name if product else "(produit supprimé)"
        end_date = promo.end_date if promo.end_date.tzinfo else promo.end_date.replace(tzinfo=UTC)
        days_left = max(0, (end_date - datetime.now(UTC)).days)
        rows.append((str(promo.id), name, f"-{promo.discount_percent}% — {days_left}j restants"))
    return [("Promotions en cours", rows), ANNULER_SECTION]


def _resolve_active_promotion(text: str, promotions: list[Promotion], names: dict[str, str]) -> Promotion | None:
    stripped = text.strip()
    if stripped.isdigit():
        idx = int(stripped)
        if 1 <= idx <= len(promotions):
            return promotions[idx - 1]
    text_lower = stripped.lower()
    for promo in promotions:
        name = names.get(str(promo.product_id), "")
        if name and (name.lower() in text_lower or text_lower in name.lower()):
            return promo
    return None


async def _catalog_message(db: AsyncSession, business: Business) -> str:
    products = await _products(db, business.id)
    if not products:
        return "Vous n'avez aucun produit dans votre catalogue."

    groups: dict[str, list[Product]] = {}
    for p in products:
        groups.setdefault(p.category or "Sans catégorie", []).append(p)

    lines = []
    for category, items in groups.items():
        lines.append(f"*{category}*")
        for p in items:
            avail = " (indisponible)" if not p.is_available else ""
            promo = await get_active_promotion(db, p.id)
            if promo:
                discounted = promo.calculate_discounted_price(p.price_xof)
                price_line = f"~{int(p.price_xof)}~ {int(discounted)} FCFA 🏷️ PROMO -{promo.discount_percent}%"
            else:
                price_line = f"{int(p.price_xof)} FCFA"
            if p.track_inventory:
                stock_line = f"Stock actuel : {p.quantity_in_stock} / initial : {p.initial_stock}"
            else:
                stock_line = "Stock non suivi"
            lines.append(f"- {p.name}{avail} — {price_line} — {stock_line}")
        lines.append("")
    return "\n".join(lines).strip()


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


async def _pending_orders(db: AsyncSession, business_id: UUID) -> list[Order]:
    return (
        await db.execute(
            select(Order)
            .where(Order.business_id == business_id, Order.status.in_([OrderStatus.PENDING, OrderStatus.CONFIRMED]))
            .order_by(Order.created_at.asc())
        )
    ).scalars().all()


def _pending_orders_list_sections(orders: list[Order]) -> list[tuple[str, list[tuple[str, str, str]]]]:
    rows = [
        (
            order.order_number,
            order.order_number,
            f"{order.customer_name or '?'} — {int(order.total_xof)} FCFA — {order.status.value}",
        )
        for order in orders
    ]
    return [("Commandes", rows), ANNULER_SECTION]


def _resolve_order(text: str, orders: list[Order]) -> Order | None:
    stripped = text.strip()
    if stripped.isdigit():
        idx = int(stripped)
        if 1 <= idx <= len(orders):
            return orders[idx - 1]
    text_upper = stripped.upper()
    for order in orders:
        if order.order_number in text_upper or text_upper in order.order_number:
            return order
    return None


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


async def handle_intent(db: AsyncSession, business: Business, conversation: Conversation, text: str) -> BotReply | None:
    """Returns None when nothing in the menu matches — the caller (engine.py)
    then tries the LLM for an ad-hoc question about the merchant's own shop
    before giving up, same as the customer side falls back past its FAQ/intent
    matches."""
    order_command_reply = await _handle_order_command(db, business, text)
    if order_command_reply is not None:
        return with_merchant_menu(order_command_reply)

    intent = detect_intent(text, is_merchant=True)

    if intent == Intent.GREETING:
        return with_merchant_menu(f"Bonjour {business.owner_name or ''} ! Que souhaitez-vous faire ?".strip())

    if intent == Intent.GOODBYE:
        return BotReply(text="À bientôt ! -- Jaaykat bi")

    if intent == Intent.VOIR_CATALOGUE:
        return with_merchant_menu(await _catalog_message(db, business))

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
            text="Comment souhaitez-vous sélectionner les produits ?",
            list_button_text="Choisir",
            list_sections=_PROMO_MODE_SECTIONS,
        )

    if intent == Intent.ARRETER_PROMOTION:
        promotions = await _active_promotions(db, business.id)
        if not promotions:
            return with_merchant_menu("Aucune promotion en cours actuellement.")
        state.start_flow(conversation, STOP_PROMOTION_FLOW)
        return BotReply(
            text="Quelle promotion souhaitez-vous arrêter ?",
            list_button_text="Choisir",
            list_sections=await _promotion_list_sections(db, promotions),
        )

    if intent == Intent.CONSULTER_VENTES:
        return with_merchant_menu(await _sales_summary(db, business))

    if intent == Intent.VOIR_COMMANDES:
        orders = await _pending_orders(db, business.id)
        if not orders:
            return with_merchant_menu("Aucune commande en attente.")
        state.start_flow(conversation, VIEW_ORDERS_FLOW)
        return BotReply(
            text="Quelle commande souhaitez-vous consulter ?",
            list_button_text="Choisir",
            list_sections=_pending_orders_list_sections(orders),
        )

    if intent == Intent.MESSAGES_EN_ATTENTE:
        return with_merchant_menu(await _pending_human_handoffs(db, business))

    return None


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
    if flow == STOP_PROMOTION_FLOW:
        return await _continue_stop_promotion(db, business, conversation, text)
    if flow == VIEW_ORDERS_FLOW:
        return await _continue_view_orders(db, business, conversation, text)

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
            initial_stock=slots["stock"],
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
        return BotReply(
            text=f"Que souhaitez-vous modifier pour {product.name} ?",
            list_button_text="Choisir",
            list_sections=_EDIT_FIELD_SECTIONS,
        )

    if step == 1:
        t = text.strip().lower()
        if t.startswith("1") or "nom" in t:
            field = "name"
        elif t.startswith("2") or "prix" in t:
            field = "price"
        elif t.startswith("3") or "stock" in t:
            field = "stock"
        elif t.startswith("4") or "disponib" in t:
            field = "availability"
        else:
            return BotReply(
                text="Que souhaitez-vous modifier ?", list_button_text="Choisir", list_sections=_EDIT_FIELD_SECTIONS
            )
        slots["field"] = field
        state.advance(conversation, 2, slots)
        if field == "availability":
            return BotReply(text="Le produit doit-il être disponible ?", buttons=_YES_NO_BUTTONS)
        prompts = {
            "name": "Quel est le nouveau nom du produit ?",
            "price": "Quel est le nouveau prix en FCFA ?",
            "stock": "Quel est le nouveau stock ?",
        }
        return with_cancel_button(prompts[field])

    if step == 2:
        product = await db.get(Product, UUID(slots["product_id"]))
        field = slots["field"]
        if field == "name":
            new_name = text.strip()
            if not new_name:
                return with_cancel_button("Merci d'indiquer un nom valide.")
            old_name = product.name
            product.name = new_name
            confirmation = f"{old_name} a été renommé en {new_name}."
        elif field == "price":
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
            product.initial_stock = product.quantity_in_stock  # a restock resets the baseline
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

    # --- Step 0: how to select which products get this promotion ---
    if step == 0:
        t = text.strip().lower()
        if "categ" in t or "catégorie" in text.lower():
            state.advance(conversation, 20, slots)
            return BotReply(
                text="Quelle catégorie souhaitez-vous mettre en promotion ?",
                list_button_text="Choisir",
                list_sections=_category_list_sections(products),
            )
        if "plusieurs" in t or "multi" in t:
            slots["product_ids"] = []
            state.advance(conversation, 30, slots)
            return BotReply(
                text="Choisissez un premier produit :",
                list_button_text="Choisir",
                list_sections=_product_list_sections(products),
            )
        if "un produit" in t or "single" in t or "seul" in t:
            state.advance(conversation, 10, slots)
            return BotReply(
                text="Quel produit souhaitez-vous mettre en promotion ?",
                list_button_text="Choisir",
                list_sections=_product_list_sections(products),
            )
        return BotReply(
            text="Comment souhaitez-vous sélectionner les produits ?",
            list_button_text="Choisir",
            list_sections=_PROMO_MODE_SECTIONS,
        )

    # --- Step 10: single product ---
    if step == 10:
        product = resolve_product(text, products)
        if product is None:
            return BotReply(
                text="Produit introuvable. Choisissez dans la liste :",
                list_button_text="Choisir",
                list_sections=_product_list_sections(products),
            )
        slots["product_ids"] = [str(product.id)]
        state.advance(conversation, 40, slots)
        return with_cancel_button("Quel pourcentage de réduction ? (ex : 10)")

    # --- Step 20: whole category ---
    if step == 20:
        categories = _distinct_categories(products)
        category = _resolve_category(text, categories)
        if category is None:
            return BotReply(
                text="Catégorie introuvable. Choisissez dans la liste :",
                list_button_text="Choisir",
                list_sections=_category_list_sections(products),
            )
        slots["category"] = category
        slots["product_ids"] = [
            str(p.id) for p in products if (p.category or "Sans catégorie") == category
        ]
        state.advance(conversation, 40, slots)
        return with_cancel_button(f"Quel pourcentage de réduction pour la catégorie « {category} » ? (ex : 10)")

    # --- Step 30/31: multi-select loop ---
    if step == 30:
        already = set(slots.get("product_ids", []))
        remaining = [p for p in products if str(p.id) not in already]
        product = resolve_product(text, remaining)
        if product is None:
            return BotReply(
                text="Produit introuvable. Choisissez dans la liste :",
                list_button_text="Choisir",
                list_sections=_product_list_sections(products, exclude_ids=already),
            )
        slots.setdefault("product_ids", []).append(str(product.id))
        state.advance(conversation, 31, slots)
        count = len(slots["product_ids"])
        return BotReply(
            text=f"{product.name} ajouté ({count} sélectionné(s)). Voulez-vous en ajouter un autre ?",
            buttons=_ADD_ANOTHER_BUTTONS,
        )

    if step == 31:
        t = text.strip().lower()
        if "ajouter" in t:
            already = set(slots.get("product_ids", []))
            remaining = [p for p in products if str(p.id) not in already]
            if not remaining:
                state.advance(conversation, 40, slots)
                return with_cancel_button("Tous les produits sont déjà sélectionnés. Quel pourcentage de réduction ?")
            state.advance(conversation, 30, slots)
            return BotReply(
                text="Choisissez un autre produit :",
                list_button_text="Choisir",
                list_sections=_product_list_sections(products, exclude_ids=already),
            )
        if "termin" in t or "fini" in t or t == "non":
            state.advance(conversation, 40, slots)
            return with_cancel_button("Quel pourcentage de réduction pour ces produits ? (ex : 10)")
        return BotReply(text="Voulez-vous ajouter un autre produit ?", buttons=_ADD_ANOTHER_BUTTONS)

    # --- Steps 40-42: shared discount % / duration / confirm, regardless of mode ---
    if step == 40:
        match = re.search(r"\d+", text)
        if not match or not (0 < int(match.group()) < 100):
            return with_cancel_button("Merci d'indiquer un pourcentage valide entre 1 et 99.")
        slots["discount_percent"] = int(match.group())
        state.advance(conversation, 41, slots)
        return BotReply(
            text="Pendant combien de jours ?", list_button_text="Choisir", list_sections=_PROMO_DURATION_SECTIONS
        )

    if step == 41:
        match = re.search(r"\d+", text)
        if not match or int(match.group()) <= 0:
            return BotReply(
                text="Pendant combien de jours ?", list_button_text="Choisir", list_sections=_PROMO_DURATION_SECTIONS
            )
        slots["duration_days"] = int(match.group())
        state.advance(conversation, 42, slots)

        product_ids = slots["product_ids"]
        selected = [p for p in products if str(p.id) in set(product_ids)]
        lines = [
            f"- {p.name} : {int(p.price_xof)} → {int(round(float(p.price_xof) * (1 - slots['discount_percent'] / 100), 2))} FCFA"
            for p in selected
        ]
        recap = (
            f"Promotion de {slots['discount_percent']}% pendant {slots['duration_days']} jours sur "
            f"{len(selected)} produit(s) :\n" + "\n".join(lines)
        )
        return BotReply(text=recap, buttons=_CONFIRM_BUTTONS)

    if step == 42:
        if "confirmer" not in text.lower():
            return BotReply(text="Voulez-vous activer cette promotion ?", buttons=_CONFIRM_BUTTONS)
        product_ids = slots["product_ids"]
        end_date = datetime.now(UTC) + timedelta(days=slots["duration_days"])
        for product_id in product_ids:
            db.add(
                Promotion(
                    business_id=business.id,
                    product_id=UUID(product_id),
                    title=f"Promo {slots['discount_percent']}%",
                    discount_percent=slots["discount_percent"],
                    duration_days=slots["duration_days"],
                    end_date=end_date,
                )
            )
        state.clear_flow(conversation)
        count = len(product_ids)
        label = "produit" if count == 1 else "produits"
        return with_merchant_menu(f"Promotion activée sur {count} {label}. -- Jaaykat bi")

    state.clear_flow(conversation)
    return with_merchant_menu("Que souhaitez-vous faire ?")


async def _continue_stop_promotion(db: AsyncSession, business: Business, conversation: Conversation, text: str) -> BotReply:
    step = state.current_step(conversation)
    slots = state.current_slots(conversation)
    promotions = await _active_promotions(db, business.id)

    if step == 0:
        names = {}
        for promo in promotions:
            product = await db.get(Product, promo.product_id)
            names[str(promo.product_id)] = product.name if product else ""
        promo = _resolve_active_promotion(text, promotions, names)
        if promo is None:
            return BotReply(
                text="Promotion introuvable. Choisissez dans la liste :",
                list_button_text="Choisir",
                list_sections=await _promotion_list_sections(db, promotions),
            )
        slots["promotion_id"] = str(promo.id)
        state.advance(conversation, 1, slots)
        product = await db.get(Product, promo.product_id)
        name = product.name if product else "ce produit"
        return BotReply(text=f"Confirmez-vous l'arrêt de la promotion sur {name} ?", buttons=_YES_NO_BUTTONS)

    if step == 1:
        t = text.strip().lower()
        if t not in {"oui", "non"}:
            return BotReply(text="Confirmez-vous l'arrêt de cette promotion ?", buttons=_YES_NO_BUTTONS)
        promo = await db.get(Promotion, UUID(slots["promotion_id"]))
        if t == "oui" and promo is not None:
            promo.is_active = False
            product = await db.get(Product, promo.product_id)
            name = product.name if product else "ce produit"
            state.clear_flow(conversation)
            return with_merchant_menu(f"Promotion arrêtée sur {name}. -- Jaaykat bi")
        state.clear_flow(conversation)
        return with_merchant_menu("D'accord, la promotion continue.")

    state.clear_flow(conversation)
    return with_merchant_menu("Que souhaitez-vous faire ?")


async def _continue_view_orders(db: AsyncSession, business: Business, conversation: Conversation, text: str) -> BotReply:
    """Step 0: pick an order from the tappable list. Step 1: a submenu of the
    valid actions for that order's current status. The typed "<ref> action"
    shorthand still works even while this flow is active (checked first in
    step 0), so power users aren't forced through the guided picker."""
    step = state.current_step(conversation)
    slots = state.current_slots(conversation)

    if step == 0:
        shortcut_reply = await _handle_order_command(db, business, text)
        if shortcut_reply is not None:
            state.clear_flow(conversation)
            return with_merchant_menu(shortcut_reply)

        orders = await _pending_orders(db, business.id)
        order = _resolve_order(text, orders)
        if order is None:
            if not orders:
                state.clear_flow(conversation)
                return with_merchant_menu("Aucune commande en attente.")
            return BotReply(
                text="Commande introuvable. Choisissez dans la liste :",
                list_button_text="Choisir",
                list_sections=_pending_orders_list_sections(orders),
            )
        slots["order_number"] = order.order_number
        state.advance(conversation, 1, slots)
        actions = _ORDER_ACTIONS_BY_STATUS.get(order.status, [])
        return BotReply(
            text=f"Commande {order.order_number} — {order.customer_name or '?'} — "
            f"{int(order.total_xof)} FCFA — {order.status.value}. Que souhaitez-vous faire ?",
            buttons=actions,
        )

    if step == 1:
        order = await db.scalar(
            select(Order).where(Order.business_id == business.id, Order.order_number == slots["order_number"])
        )
        if order is None:
            state.clear_flow(conversation)
            return with_merchant_menu("Cette commande est introuvable.")

        t = text.strip().lower()

        if "retour" in t:
            orders = await _pending_orders(db, business.id)
            if not orders:
                state.clear_flow(conversation)
                return with_merchant_menu("Aucune commande en attente.")
            state.advance(conversation, 0, {})
            return BotReply(
                text="Quelle commande souhaitez-vous consulter ?",
                list_button_text="Choisir",
                list_sections=_pending_orders_list_sections(orders),
            )

        if "confirmer" in t and order.status == OrderStatus.PENDING:
            await confirm_order(order)
            state.clear_flow(conversation)
            return with_merchant_menu(f"{order.order_number} marquée comme confirmée.")

        if "livr" in t and order.status == OrderStatus.CONFIRMED:  # matches "livrer" (typed) and "livrée" (button)
            await fulfill_order(order)
            state.clear_flow(conversation)
            return with_merchant_menu(f"{order.order_number} marquée comme livrée. Merci !")

        if "annuler la commande" in t and order.status in (OrderStatus.PENDING, OrderStatus.CONFIRMED):
            await cancel_order(db, order, reason="Annulée par le marchand via WhatsApp")
            state.clear_flow(conversation)
            return with_merchant_menu(f"{order.order_number} a été annulée.")

        actions = _ORDER_ACTIONS_BY_STATUS.get(order.status, [])
        if not actions:
            state.clear_flow(conversation)
            return with_merchant_menu(f"{order.order_number} est déjà {order.status.value}.")
        return BotReply(text="Que souhaitez-vous faire ?", buttons=actions)

    state.clear_flow(conversation)
    return with_merchant_menu("Que souhaitez-vous faire ?")
