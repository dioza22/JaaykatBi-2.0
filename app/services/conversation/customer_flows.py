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
from app.services.conversation.reply import ANNULER_SECTION, BotReply, cap_total_rows, with_cancel_button, with_customer_menu
from app.services.orders.service import (
    InsufficientStockError,
    cancel_order,
    create_order,
    effective_price,
    update_order_quantity,
)

ORDER_FLOW = "commander_produit"
RETURN_REQUEST_FLOW = "demander_retour"
MY_ORDERS_FLOW = "mes_commandes_client"


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
    sections = cap_total_rows([("Produits", rows)])
    if include_cancel:
        sections.append(ANNULER_SECTION)
    return sections


async def catalog_message(db: AsyncSession, business: Business, conversation: Conversation) -> BotReply:
    """Selecting a product row here starts the order flow for that exact
    product (step 0's product-resolution already handles a tapped row's
    title the same way it handles typed text) — browsing the catalog and
    starting an order are the same action from the customer's side."""
    reply = await start_order_flow(db, business, conversation)
    if reply.list_sections:
        reply.text = "Voici notre catalogue. Sélectionnez un produit pour passer commande."
    return reply


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
            # Deliberately doesn't reveal the exact quantity_in_stock — remaining
            # inventory is merchant-internal data, not something a customer needs.
            return with_cancel_button(
                f"Cette quantité n'est pas disponible pour {product.name}. Merci d'indiquer une quantité plus faible."
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


def _find_returnable_order(text: str, orders: list[Order]) -> Order | None:
    """Unlike resolve_order, no numeric-index matching — the customer types
    their own order number here (no list is shown, by design: a return/
    refund request requires them to know and state their reference)."""
    stripped = text.strip().upper()
    for order in orders:
        if order.order_number.upper() == stripped or stripped in order.order_number.upper():
            return order
    return None


async def start_return_flow(db: AsyncSession, contact: Contact, conversation: Conversation) -> BotReply:
    orders = await _returnable_orders(db, contact.id)
    if not orders:
        return with_customer_menu("Vous n'avez pas de commande pouvant faire l'objet d'un retour. -- Jaaykat bi")
    state.start_flow(conversation, RETURN_REQUEST_FLOW)
    return with_cancel_button("Veuillez indiquer le numéro de la commande à retourner (ex : CMD-20260728-0001).")


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
        order = _find_returnable_order(text, orders)
        if order is None:
            return with_cancel_button(
                "Commande introuvable ou non éligible au retour. Merci de vérifier le numéro (ex : CMD-20260728-0001)."
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


_ONGOING_ORDER_ACTIONS = [("update", "Mettre à jour"), ("cancel_order", "Annuler la commande"), ("back", "Retour")]


async def _ongoing_orders(db: AsyncSession, contact_id: UUID) -> list[Order]:
    """Only PENDING/CONFIRMED — fulfilled orders aren't "ongoing" and are
    handled through the separate return/refund flow instead."""
    orders: list[Order] = []
    for status in (OrderStatus.PENDING, OrderStatus.CONFIRMED):
        batch = (
            await db.execute(
                select(Order)
                .where(Order.contact_id == contact_id, Order.status == status)
                .order_by(Order.created_at.asc())
                .limit(10)
            )
        ).scalars().all()
        orders.extend(batch)
    return orders


def _ongoing_orders_list_sections(orders: list[Order]) -> list[tuple[str, list[tuple[str, str, str]]]]:
    by_status = {OrderStatus.PENDING: [], OrderStatus.CONFIRMED: []}
    for order in orders:
        by_status.setdefault(order.status, []).append(order)
    labels = {OrderStatus.PENDING: "En attente", OrderStatus.CONFIRMED: "Confirmées"}
    sections = []
    for status in (OrderStatus.PENDING, OrderStatus.CONFIRMED):
        rows = [
            (order.order_number, order.order_number, f"{int(order.total_xof)} FCFA — {order.status.value}")
            for order in by_status[status]
        ]
        if rows:
            sections.append((labels[status], rows))
    return cap_total_rows(sections) + [ANNULER_SECTION]


async def _get_contact_order(db: AsyncSession, contact_id: UUID, order_number: str | None) -> Order | None:
    if not order_number:
        return None
    return await db.scalar(select(Order).where(Order.contact_id == contact_id, Order.order_number == order_number))


def _update_field_sections(order: Order) -> list[tuple[str, list[tuple[str, str, str]]]]:
    rows = [("qty", "Quantité", "Changer la quantité commandée")]
    if order.delivery_type == DeliveryType.DELIVERY:
        rows.append(("address", "Adresse", "Changer l'adresse de livraison"))
    rows.append(("name", "Nom", "Corriger le nom associé à la commande"))
    return cap_total_rows([("Modifier", rows)]) + [ANNULER_SECTION]


async def start_my_orders_flow(db: AsyncSession, contact: Contact, conversation: Conversation) -> BotReply:
    orders = await _ongoing_orders(db, contact.id)
    if not orders:
        return with_customer_menu("Vous n'avez aucune commande en cours actuellement. -- Jaaykat bi")
    state.start_flow(conversation, MY_ORDERS_FLOW)
    return BotReply(
        text="Quelle commande souhaitez-vous consulter ?",
        list_button_text="Choisir",
        list_sections=_ongoing_orders_list_sections(orders),
    )


async def continue_my_orders_flow(
    db: AsyncSession,
    business: Business,
    contact: Contact,
    conversation: Conversation,
    text: str,
) -> BotReply:
    """Step 0: pick an order. Step 1: submenu (update/cancel/back). Step 10:
    which field to update. Step 11: apply the update. Step 21: confirm a
    late/post-confirmation cancellation after the fee has been disclosed."""
    step = state.current_step(conversation)
    slots = state.current_slots(conversation)

    if step == 0:
        orders = await _ongoing_orders(db, contact.id)
        order = resolve_order(text, orders)
        if order is None:
            if not orders:
                state.clear_flow(conversation)
                return with_customer_menu("Vous n'avez aucune commande en cours actuellement. -- Jaaykat bi")
            return BotReply(
                text="Commande introuvable. Choisissez dans la liste :",
                list_button_text="Choisir",
                list_sections=_ongoing_orders_list_sections(orders),
            )
        slots["order_number"] = order.order_number
        state.advance(conversation, 1, slots)
        return BotReply(
            text=f"Commande {order.order_number} — {int(order.total_xof)} FCFA — {order.status.value}. "
            f"Que souhaitez-vous faire ?",
            buttons=_ONGOING_ORDER_ACTIONS,
        )

    if step == 1:
        order = await _get_contact_order(db, contact.id, slots.get("order_number"))
        if order is None:
            state.clear_flow(conversation)
            return with_customer_menu("Cette commande est introuvable.")
        t = text.strip().lower()

        if "retour" in t:
            orders = await _ongoing_orders(db, contact.id)
            if not orders:
                state.clear_flow(conversation)
                return with_customer_menu("Vous n'avez aucune commande en cours actuellement. -- Jaaykat bi")
            state.advance(conversation, 0, {})
            return BotReply(
                text="Quelle commande souhaitez-vous consulter ?",
                list_button_text="Choisir",
                list_sections=_ongoing_orders_list_sections(orders),
            )

        if "mettre à jour" in t or "mettre a jour" in t:
            state.advance(conversation, 10, slots)
            return BotReply(
                text="Que souhaitez-vous modifier ?",
                list_button_text="Choisir",
                list_sections=_update_field_sections(order),
            )

        if "annuler la commande" in t:
            if order.is_freely_cancellable():
                await cancel_order(db, order, reason="Annulée par le client via WhatsApp")
                state.clear_flow(conversation)
                merchant_notification = (
                    f"🔔 Commande {order.order_number} annulée par le client. "
                    f"Répondez 'mes commandes' pour plus de détails."
                )
                return with_customer_menu(
                    f"Votre commande {order.order_number} a été annulée. -- Jaaykat bi",
                    merchant_notification=merchant_notification,
                )
            state.advance(conversation, 21, slots)
            fee_percent = business.late_cancellation_fee_percent or 0
            if fee_percent > 0:
                fee_amount = int(round(float(order.total_xof) * fee_percent / 100))
                text_out = (
                    "Cette commande ne peut plus être annulée gratuitement (délai de 24h dépassé ou commande "
                    f"déjà confirmée). Des frais d'annulation de {fee_percent}% ({fee_amount} FCFA) s'appliquent. "
                    f"Confirmez-vous l'annulation ?"
                )
            else:
                text_out = (
                    "Cette commande ne peut plus être annulée gratuitement (délai de 24h dépassé ou commande "
                    "déjà confirmée). Confirmez-vous tout de même l'annulation ?"
                )
            return BotReply(text=text_out, buttons=_CONFIRM_BUTTONS)

        return BotReply(text="Que souhaitez-vous faire ?", buttons=_ONGOING_ORDER_ACTIONS)

    if step == 10:
        order = await _get_contact_order(db, contact.id, slots.get("order_number"))
        if order is None:
            state.clear_flow(conversation)
            return with_customer_menu("Cette commande est introuvable.")
        t = text.strip().lower()

        if "quantit" in t:
            slots["update_field"] = "quantity"
            state.advance(conversation, 11, slots)
            item = order.items[0]
            return with_cancel_button(
                f"Quelle nouvelle quantité pour {item.product_name} ? (actuellement {item.quantity})"
            )

        if "adresse" in t:
            if order.delivery_type != DeliveryType.DELIVERY:
                return BotReply(
                    text="Cette commande est un retrait en boutique, il n'y a pas d'adresse à modifier.",
                    list_button_text="Choisir",
                    list_sections=_update_field_sections(order),
                )
            slots["update_field"] = "address"
            state.advance(conversation, 11, slots)
            return with_cancel_button(
                f"Quelle est la nouvelle adresse de livraison ? (actuelle : {order.delivery_address})"
            )

        if "nom" in t:
            slots["update_field"] = "name"
            state.advance(conversation, 11, slots)
            return with_cancel_button(
                f"Quel est le nom correct pour cette commande ? (actuel : {order.customer_name})"
            )

        return BotReply(
            text="Que souhaitez-vous modifier ?", list_button_text="Choisir", list_sections=_update_field_sections(order)
        )

    if step == 11:
        order = await _get_contact_order(db, contact.id, slots.get("order_number"))
        if order is None:
            state.clear_flow(conversation)
            return with_customer_menu("Cette commande est introuvable.")
        field = slots.get("update_field")

        if field == "quantity":
            new_quantity = _extract_quantity(text)
            if new_quantity is None:
                return with_cancel_button("Merci d'indiquer une quantité valide (ex : 2).")
            try:
                await update_order_quantity(db, order, contact, new_quantity)
            except InsufficientStockError as exc:
                return with_cancel_button(f"{exc} Merci d'indiquer une quantité plus faible.")
            state.clear_flow(conversation)
            item = order.items[0]
            merchant_notification = (
                f"🔔 Commande {order.order_number} modifiée par le client : quantité → {new_quantity}. "
                f"Répondez 'mes commandes' pour la traiter."
            )
            return with_customer_menu(
                f"La quantité de votre commande {order.order_number} a été mise à jour "
                f"({new_quantity} x {item.product_name}, {int(order.total_xof)} FCFA). -- Jaaykat bi",
                merchant_notification=merchant_notification,
            )

        if field == "name":
            new_name = text.strip()
            if not new_name:
                return with_cancel_button("Merci d'indiquer un nom valide.")
            order.customer_name = new_name
            state.clear_flow(conversation)
            merchant_notification = (
                f"🔔 Commande {order.order_number} modifiée par le client : nouveau nom → {new_name}. "
                f"Répondez 'mes commandes' pour la traiter."
            )
            return with_customer_menu(
                f"Le nom associé à votre commande {order.order_number} a été mis à jour ({new_name}). -- Jaaykat bi",
                merchant_notification=merchant_notification,
            )

        new_address = text.strip()
        if not new_address:
            return with_cancel_button("Merci d'indiquer une adresse valide.")
        order.delivery_address = new_address
        state.clear_flow(conversation)
        merchant_notification = (
            f"🔔 Commande {order.order_number} modifiée par le client : nouvelle adresse de livraison. "
            f"Répondez 'mes commandes' pour la traiter."
        )
        return with_customer_menu(
            f"L'adresse de livraison de votre commande {order.order_number} a été mise à jour. -- Jaaykat bi",
            merchant_notification=merchant_notification,
        )

    if step == 21:
        order = await _get_contact_order(db, contact.id, slots.get("order_number"))
        if order is None:
            state.clear_flow(conversation)
            return with_customer_menu("Cette commande est introuvable.")
        if "confirmer" in text.lower() or text.strip().lower() == "oui":
            fee_percent = business.late_cancellation_fee_percent or 0
            fee_amount = int(round(float(order.total_xof) * fee_percent / 100)) if fee_percent else 0
            await cancel_order(
                db, order, reason="Annulée par le client via WhatsApp (hors délai)", fee_xof=fee_amount or None
            )
            state.clear_flow(conversation)
            fee_line = f" Des frais de {fee_amount} FCFA restent dus au commerçant." if fee_amount else ""
            merchant_notification = f"🔔 Commande {order.order_number} annulée par le client (hors délai)." + (
                f" {fee_amount} FCFA de frais à recouvrer." if fee_amount else ""
            )
            return with_customer_menu(
                f"Votre commande {order.order_number} a été annulée.{fee_line} -- Jaaykat bi",
                merchant_notification=merchant_notification,
            )
        state.clear_flow(conversation)
        return with_customer_menu("D'accord, la commande n'a pas été annulée. -- Jaaykat bi")

    state.clear_flow(conversation)
    return with_customer_menu("Une erreur est survenue, reprenons depuis le début.")


async def continue_flow(
    db: AsyncSession,
    business: Business,
    contact: Contact,
    conversation: Conversation,
    text: str,
) -> BotReply:
    flow = state.current_flow(conversation)
    if flow == RETURN_REQUEST_FLOW:
        return await continue_return_flow(db, contact, conversation, text)
    if flow == MY_ORDERS_FLOW:
        return await continue_my_orders_flow(db, business, contact, conversation, text)
    return await continue_order_flow(db, business, contact, conversation, text)
