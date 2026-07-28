"""Customer-facing flows: catalog Q&A and the order-taking FSM. This is the
fix for the old build's biggest gap — flow completion calls
`orders.service.create_order()` directly instead of just talking about it.

Interactive elements (buttons/lists) are used wherever the customer is
picking from a bounded set of options — see reply.py for BotReply and
menu helpers, and the module docstring there for why a button/list title
can double as the text intent-matching still relies on."""

import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, Contact, Conversation, DeliveryType, Order, OrderStatus, PaymentMethod, Product
from app.services.conversation import state
from app.services.conversation.order_lookup import resolve_order
from app.services.conversation.product_lookup import resolve_product
from app.services.conversation.reply import ANNULER_SECTION, BotReply, with_cancel_button, with_customer_menu
from app.services.orders.service import InsufficientStockError, create_order, effective_price

ORDER_FLOW = "commander_produit"
RETURN_REQUEST_FLOW = "demander_retour"


async def available_products(db: AsyncSession, business_id: UUID) -> list[Product]:
    return (
        await db.execute(
            select(Product)
            .where(Product.business_id == business_id, Product.is_available == True)  # noqa: E712
            .order_by(Product.display_order, Product.name)
        )
    ).scalars().all()


async def _format_catalog(db: AsyncSession, products: list[Product]) -> str:
    if not products:
        return "(catalogue non disponible pour le moment)"
    lines = []
    for i, product in enumerate(products, start=1):
        price = await effective_price(db, product)
        if price < float(product.price_xof):
            lines.append(f"{i}. {product.name} — ~~{int(product.price_xof)}~~ {int(price)} FCFA (promo)")
        else:
            lines.append(f"{i}. {product.name} — {int(price)} FCFA")
    return "\n".join(lines)


async def _product_list_sections(
    db: AsyncSession, products: list[Product], include_cancel: bool = False
) -> list[tuple[str, list[tuple[str, str, str]]]]:
    rows = []
    for product in products:
        price = await effective_price(db, product)
        if price < float(product.price_xof):
            description = f"~{int(product.price_xof)}~ {int(price)} FCFA (promo)"
        else:
            description = f"{int(price)} FCFA"
        rows.append((str(product.id), product.name, description))
    sections = [("Produits", rows)]
    if include_cancel:
        sections.append(ANNULER_SECTION)
    return sections


async def catalog_message(db: AsyncSession, business: Business) -> BotReply:
    products = await available_products(db, business.id)
    if not products:
        return BotReply(text="Notre catalogue est momentanément indisponible. Merci de réessayer plus tard. -- Jaaykat bi")
    sections = await _product_list_sections(db, products)
    return BotReply(
        text="Voici notre catalogue. Répondez 'commander' quand vous êtes prêt à passer commande.",
        list_button_text="Catalogue",
        list_sections=sections,
    )


async def promotions_message(db: AsyncSession, business: Business) -> BotReply:
    products = await available_products(db, business.id)
    promo_lines = []
    for product in products:
        price = await effective_price(db, product)
        if price < float(product.price_xof):
            promo_lines.append(f"- {product.name} : {int(price)} FCFA (au lieu de {int(product.price_xof)} FCFA)")
    if not promo_lines:
        return BotReply(text="Nous n'avons pas de promotion en cours pour le moment. -- Jaaykat bi")
    return BotReply(text="Nos promotions en cours :\n" + "\n".join(promo_lines))


def _extract_quantity(text: str) -> int | None:
    match = re.search(r"\d+", text)
    if not match:
        return None
    value = int(match.group())
    return value if value > 0 else None


def _resolve_payment_method(text: str) -> PaymentMethod | None:
    t = text.strip().lower()
    if t.startswith("1") or "wave" in t:
        return PaymentMethod.WAVE
    if t.startswith("2") or "orange" in t:
        return PaymentMethod.ORANGE_MONEY
    if t.startswith("3") or "cash" in t or "espèce" in t or "espece" in t:
        return PaymentMethod.CASH
    return None


# Payment method is a list, not buttons — 3 real options already fills
# WhatsApp's button cap, leaving no room for a 4th "Annuler" button.
_PAYMENT_SECTIONS = [
    ("Paiement", [("wave", "Wave", ""), ("orange_money", "Orange Money", ""), ("cash", "Cash à la livraison", "")]),
    ANNULER_SECTION,
]
_DELIVERY_TYPE_BUTTONS = [("pickup", "Retrait en boutique"), ("delivery", "Livraison"), ("annuler", "Annuler")]
_CONFIRM_BUTTONS = [("confirm", "Confirmer"), ("cancel", "Annuler")]


async def start_order_flow(db: AsyncSession, business: Business, conversation: Conversation) -> BotReply:
    products = await available_products(db, business.id)
    if not products:
        return BotReply(text="Notre catalogue est momentanément indisponible pour passer commande. -- Jaaykat bi")
    state.start_flow(conversation, ORDER_FLOW)
    sections = await _product_list_sections(db, products, include_cancel=True)
    return BotReply(
        text="Très bien, passons commande. Quel produit souhaitez-vous ?",
        list_button_text="Choisir",
        list_sections=sections,
    )


async def continue_order_flow(
    db: AsyncSession,
    business: Business,
    contact: Contact,
    conversation: Conversation,
    text: str,
) -> BotReply:
    step = state.current_step(conversation)
    slots = state.current_slots(conversation)
    products = await available_products(db, business.id)

    if step == 0:
        product = resolve_product(text, products)
        if product is None:
            sections = await _product_list_sections(db, products, include_cancel=True)
            return BotReply(
                text="Je n'ai pas trouvé ce produit. Merci de choisir dans la liste :",
                list_button_text="Choisir",
                list_sections=sections,
            )
        price = await effective_price(db, product)
        slots["product_id"] = str(product.id)
        state.advance(conversation, 1, slots)
        return with_cancel_button(f"{product.name} à {int(price)} FCFA l'unité. Combien d'unités souhaitez-vous ?")

    if step == 1:
        quantity = _extract_quantity(text)
        product = await db.get(Product, UUID(slots["product_id"]))
        if quantity is None:
            return with_cancel_button("Merci d'indiquer une quantité valide (ex : 2).")
        if product.track_inventory and product.quantity_in_stock is not None and quantity > product.quantity_in_stock:
            return with_cancel_button(
                f"Il ne reste que {product.quantity_in_stock} unité(s) de {product.name}. Quelle quantité souhaitez-vous ?"
            )
        slots["quantity"] = quantity
        state.advance(conversation, 2, slots)
        return with_cancel_button("Quel est votre nom complet pour la commande ?")

    if step == 2:
        name = text.strip()
        if not name:
            return with_cancel_button("Merci d'indiquer votre nom complet.")
        slots["customer_name"] = name
        state.advance(conversation, 3, slots)
        return BotReply(text="Souhaitez-vous une livraison ou un retrait en boutique ?", buttons=_DELIVERY_TYPE_BUTTONS)

    if step == 3:
        if "retrait" in text.lower():
            slots["delivery_type"] = DeliveryType.PICKUP.value
            slots["delivery_address"] = None
            state.advance(conversation, 5, slots)
            return BotReply(
                text="Comment souhaitez-vous payer ?", list_button_text="Choisir", list_sections=_PAYMENT_SECTIONS
            )
        slots["delivery_type"] = DeliveryType.DELIVERY.value
        state.advance(conversation, 4, slots)
        return with_cancel_button("Quelle est votre adresse de livraison ?")

    if step == 4:
        slots["delivery_address"] = text.strip()
        state.advance(conversation, 5, slots)
        return BotReply(
            text="Comment souhaitez-vous payer ?", list_button_text="Choisir", list_sections=_PAYMENT_SECTIONS
        )

    if step == 5:
        payment_method = _resolve_payment_method(text)
        if payment_method is None:
            return BotReply(
                text="Merci de choisir un mode de paiement.",
                list_button_text="Choisir",
                list_sections=_PAYMENT_SECTIONS,
            )
        slots["payment_method"] = payment_method.value
        state.advance(conversation, 6, slots)
        product = await db.get(Product, UUID(slots["product_id"]))
        price = await effective_price(db, product)
        total = price * slots["quantity"]
        delivery_line = (
            "Retrait en boutique" if slots["delivery_type"] == DeliveryType.PICKUP.value
            else f"Livraison à : {slots['delivery_address']}"
        )
        recap = (
            f"Récapitulatif de votre commande :\n"
            f"- {slots['quantity']} x {product.name} = {int(total)} FCFA\n"
            f"- {delivery_line}\n"
            f"- Paiement : {payment_method.value}\n"
            f"- Client : {slots['customer_name']}"
        )
        return BotReply(text=recap, buttons=_CONFIRM_BUTTONS)

    if step == 6:
        if "confirmer" in text.lower() or text.strip().lower() == "oui":
            product = await db.get(Product, UUID(slots["product_id"]))
            try:
                order = await create_order(
                    db,
                    business,
                    contact,
                    product,
                    slots["quantity"],
                    slots["customer_name"],
                    DeliveryType(slots["delivery_type"]),
                    slots.get("delivery_address"),
                    PaymentMethod(slots["payment_method"]),
                )
            except InsufficientStockError as exc:
                state.clear_flow(conversation)
                return with_customer_menu(f"{exc} Votre commande n'a pas pu être enregistrée. -- Jaaykat bi")
            state.clear_flow(conversation)
            delivery_line = (
                "retrait en boutique" if order.delivery_type == DeliveryType.PICKUP
                else f"livraison à {order.delivery_address}"
            )
            merchant_notification = (
                f"🔔 Nouvelle commande {order.order_number} de {order.customer_name} — "
                f"{int(order.total_xof)} FCFA ({delivery_line}). Répondez 'mes commandes' pour la traiter."
            )
            return with_customer_menu(
                f"Votre commande a été enregistrée sous la référence {order.order_number}. "
                f"Merci pour votre confiance. -- Jaaykat bi",
                merchant_notification=merchant_notification,
            )
        return BotReply(text="Souhaitez-vous confirmer votre commande ?", buttons=_CONFIRM_BUTTONS)

    # Unknown step — shouldn't happen, but fail safe rather than loop forever.
    state.clear_flow(conversation)
    return with_customer_menu("Une erreur est survenue, reprenons depuis le début.")


async def _returnable_orders(db: AsyncSession, contact_id: UUID) -> list[Order]:
    return (
        await db.execute(
            select(Order)
            .where(
                Order.contact_id == contact_id,
                Order.status == OrderStatus.FULFILLED,
                Order.return_requested_at.is_(None),
            )
            .order_by(Order.fulfilled_at.desc())
            .limit(10)
        )
    ).scalars().all()


def _returnable_orders_list_sections(orders: list[Order]) -> list[tuple[str, list[tuple[str, str, str]]]]:
    rows = [(order.order_number, order.order_number, f"{int(order.total_xof)} FCFA") for order in orders]
    return [("Commandes livrées", rows), ANNULER_SECTION]


async def start_return_flow(db: AsyncSession, contact: Contact, conversation: Conversation) -> BotReply:
    orders = await _returnable_orders(db, contact.id)
    if not orders:
        return with_customer_menu("Vous n'avez pas de commande pouvant faire l'objet d'un retour. -- Jaaykat bi")
    state.start_flow(conversation, RETURN_REQUEST_FLOW)
    sections = _returnable_orders_list_sections(orders)
    return BotReply(
        text="Quelle commande souhaitez-vous retourner ?",
        list_button_text="Choisir",
        list_sections=sections,
    )


async def continue_return_flow(
    db: AsyncSession,
    contact: Contact,
    conversation: Conversation,
    text: str,
) -> BotReply:
    step = state.current_step(conversation)
    slots = state.current_slots(conversation)

    if step == 0:
        orders = await _returnable_orders(db, contact.id)
        order = resolve_order(text, orders)
        if order is None:
            sections = _returnable_orders_list_sections(orders)
            return BotReply(
                text="Je n'ai pas trouvé cette commande. Merci de choisir dans la liste :",
                list_button_text="Choisir",
                list_sections=sections,
            )
        slots["order_id"] = str(order.id)
        state.advance(conversation, 1, slots)
        return BotReply(
            text=f"Confirmez-vous vouloir retourner la commande {order.order_number} ({int(order.total_xof)} FCFA) ?",
            buttons=_CONFIRM_BUTTONS,
        )

    if step == 1:
        if "confirmer" in text.lower() or text.strip().lower() == "oui":
            order = await db.get(Order, UUID(slots["order_id"]))
            order.request_return()
            state.clear_flow(conversation)
            merchant_notification = (
                f"🔔 Demande de retour pour la commande {order.order_number} "
                f"({int(order.total_xof)} FCFA). Répondez 'mes commandes' pour la traiter."
            )
            return with_customer_menu(
                f"Votre demande de retour pour la commande {order.order_number} a été transmise au commerçant. "
                f"-- Jaaykat bi",
                merchant_notification=merchant_notification,
            )
        state.clear_flow(conversation)
        return with_customer_menu("Demande de retour annulée. -- Jaaykat bi")

    # Unknown step — shouldn't happen, but fail safe rather than loop forever.
    state.clear_flow(conversation)
    return with_customer_menu("Une erreur est survenue, reprenons depuis le début.")


async def continue_flow(
    db: AsyncSession,
    business: Business,
    contact: Contact,
    conversation: Conversation,
    text: str,
) -> BotReply:
    if state.current_flow(conversation) == RETURN_REQUEST_FLOW:
        return await continue_return_flow(db, contact, conversation, text)
    return await continue_order_flow(db, business, contact, conversation, text)
