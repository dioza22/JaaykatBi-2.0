"""Customer-facing flows: catalog Q&A and the order-taking FSM. This is the
fix for the old build's biggest gap — flow completion calls
`orders.service.create_order()` directly instead of just talking about it."""

import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, Contact, Conversation, DeliveryType, PaymentMethod, Product
from app.services.conversation import state
from app.services.conversation.product_lookup import resolve_product
from app.services.orders.service import InsufficientStockError, create_order, effective_price

ORDER_FLOW = "commander_produit"


async def available_products(db: AsyncSession, business_id: UUID) -> list[Product]:
    return (
        await db.execute(
            select(Product)
            .where(Product.business_id == business_id, Product.is_available == True)  # noqa: E712
            .order_by(Product.display_order, Product.name)
        )
    ).scalars().all()


async def _format_catalog(db: AsyncSession, products: list[Product]) -> str:
    lines = []
    for i, product in enumerate(products, start=1):
        price = await effective_price(db, product)
        if price < float(product.price_xof):
            lines.append(f"{i}. {product.name} — ~~{int(product.price_xof)}~~ {int(price)} FCFA (promo)")
        else:
            lines.append(f"{i}. {product.name} — {int(price)} FCFA")
    return "\n".join(lines)


async def catalog_message(db: AsyncSession, business: Business) -> str:
    products = await available_products(db, business.id)
    if not products:
        return "Notre catalogue est momentanément indisponible. Merci de réessayer plus tard. -- Jaaykat bi"
    catalog = await _format_catalog(db, products)
    return f"Voici notre catalogue :\n{catalog}\n\nRépondez avec le numéro ou le nom du produit pour commander."


async def promotions_message(db: AsyncSession, business: Business) -> str:
    products = await available_products(db, business.id)
    promo_lines = []
    for product in products:
        price = await effective_price(db, product)
        if price < float(product.price_xof):
            promo_lines.append(f"- {product.name} : {int(price)} FCFA (au lieu de {int(product.price_xof)} FCFA)")
    if not promo_lines:
        return "Nous n'avons pas de promotion en cours pour le moment. -- Jaaykat bi"
    return "Nos promotions en cours :\n" + "\n".join(promo_lines)


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


async def start_order_flow(db: AsyncSession, business: Business, conversation: Conversation) -> str:
    products = await available_products(db, business.id)
    if not products:
        return "Notre catalogue est momentanément indisponible pour passer commande. -- Jaaykat bi"
    state.start_flow(conversation, ORDER_FLOW)
    catalog = await _format_catalog(db, products)
    return f"Très bien, passons commande. Quel produit souhaitez-vous ?\n{catalog}"


async def continue_order_flow(
    db: AsyncSession,
    business: Business,
    contact: Contact,
    conversation: Conversation,
    text: str,
) -> str:
    step = state.current_step(conversation)
    slots = state.current_slots(conversation)
    products = await available_products(db, business.id)

    if step == 0:
        product = resolve_product(text, products)
        if product is None:
            catalog = await _format_catalog(db, products)
            return f"Je n'ai pas trouvé ce produit. Merci de choisir un numéro ou un nom dans la liste :\n{catalog}"
        price = await effective_price(db, product)
        slots["product_id"] = str(product.id)
        state.advance(conversation, 1, slots)
        return f"{product.name} à {int(price)} FCFA l'unité. Combien d'unités souhaitez-vous ?"

    if step == 1:
        quantity = _extract_quantity(text)
        product = await db.get(Product, UUID(slots["product_id"]))
        if quantity is None:
            return "Merci d'indiquer une quantité valide (ex : 2)."
        if product.track_inventory and product.quantity_in_stock is not None and quantity > product.quantity_in_stock:
            return f"Il ne reste que {product.quantity_in_stock} unité(s) de {product.name}. Quelle quantité souhaitez-vous ?"
        slots["quantity"] = quantity
        state.advance(conversation, 2, slots)
        return "Quel est votre nom complet pour la commande ?"

    if step == 2:
        name = text.strip()
        if not name:
            return "Merci d'indiquer votre nom complet."
        slots["customer_name"] = name
        state.advance(conversation, 3, slots)
        return "Souhaitez-vous une livraison ou un retrait en boutique ? Si livraison, indiquez votre adresse (sinon répondez 'retrait')."

    if step == 3:
        if "retrait" in text.lower():
            slots["delivery_type"] = DeliveryType.PICKUP.value
            slots["delivery_address"] = None
        else:
            slots["delivery_type"] = DeliveryType.DELIVERY.value
            slots["delivery_address"] = text.strip()
        state.advance(conversation, 4, slots)
        return "Comment souhaitez-vous payer ?\n1️⃣ Wave\n2️⃣ Orange Money\n3️⃣ Cash à la livraison"

    if step == 4:
        payment_method = _resolve_payment_method(text)
        if payment_method is None:
            return "Merci de répondre 1 (Wave), 2 (Orange Money) ou 3 (Cash à la livraison)."
        slots["payment_method"] = payment_method.value
        state.advance(conversation, 5, slots)
        product = await db.get(Product, UUID(slots["product_id"]))
        price = await effective_price(db, product)
        total = price * slots["quantity"]
        delivery_line = (
            "Retrait en boutique" if slots["delivery_type"] == DeliveryType.PICKUP.value
            else f"Livraison à : {slots['delivery_address']}"
        )
        return (
            f"Récapitulatif de votre commande :\n"
            f"- {slots['quantity']} x {product.name} = {int(total)} FCFA\n"
            f"- {delivery_line}\n"
            f"- Paiement : {payment_method.value}\n"
            f"- Client : {slots['customer_name']}\n\n"
            f"Répondez 'confirmer' pour valider, ou 'annuler' pour annuler."
        )

    if step == 5:
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
                return f"{exc} Votre commande n'a pas pu être enregistrée. -- Jaaykat bi"
            state.clear_flow(conversation)
            return (
                f"Votre commande a été enregistrée sous la référence {order.order_number}. "
                f"Merci pour votre confiance. -- Jaaykat bi"
            )
        return "Répondez 'confirmer' pour valider votre commande, ou 'annuler' pour annuler."

    # Unknown step — shouldn't happen, but fail safe rather than loop forever.
    state.clear_flow(conversation)
    return "Une erreur est survenue, reprenons depuis le début. Répondez 'commander' pour passer une nouvelle commande."
