import uuid
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, Contact, DeliveryType, Order, OrderItem, PaymentMethod, Product, Promotion


class InsufficientStockError(Exception):
    pass


async def get_active_promotion(db: AsyncSession, product_id: uuid.UUID) -> Promotion | None:
    promos = (
        await db.execute(select(Promotion).where(Promotion.product_id == product_id, Promotion.is_active == True))  # noqa: E712
    ).scalars().all()
    for promo in promos:
        if promo.is_currently_active():
            return promo
    return None


async def effective_price(db: AsyncSession, product: Product) -> float:
    """Catalog Q&A and order-taking both call this so a customer is always
    quoted (and charged) the same, promotion-aware price."""
    promo = await get_active_promotion(db, product.id)
    if promo:
        return promo.calculate_discounted_price(product.price_xof)
    return float(product.price_xof)


async def _next_order_number(db: AsyncSession, business_id: uuid.UUID) -> str:
    today = date.today()
    count = await db.scalar(
        select(func.count())
        .select_from(Order)
        .where(Order.business_id == business_id, func.date(Order.created_at) == today)
    )
    return Order.generate_order_number(datetime.now(UTC), (count or 0) + 1)


async def create_order(
    db: AsyncSession,
    business: Business,
    contact: Contact,
    product: Product,
    quantity: int,
    customer_name: str,
    delivery_type: DeliveryType,
    delivery_address: str | None,
    payment_method: PaymentMethod,
) -> Order:
    if product.track_inventory and not product.is_in_stock():
        raise InsufficientStockError(f"{product.name} est en rupture de stock.")
    if product.track_inventory and product.quantity_in_stock is not None and quantity > product.quantity_in_stock:
        # Doesn't reveal the exact quantity_in_stock — merchant-internal data,
        # never surfaced to a customer (see continue_order_flow's own
        # proactive check for the same rule; this is the race-condition
        # fallback for stock that changed between that check and confirmation).
        raise InsufficientStockError(f"Cette quantité n'est plus disponible pour {product.name}.")

    unit_price = await effective_price(db, product)

    order = Order(
        business_id=business.id,
        contact_id=contact.id,
        order_number=await _next_order_number(db, business.id),
        customer_name=customer_name,
        delivery_type=delivery_type,
        delivery_address=delivery_address,
        payment_method=payment_method,
    )
    order.items.append(OrderItem.from_product(product, quantity, unit_price))
    order.calculate_total()

    product.deduct_stock(quantity)
    contact.increment_order_stats(order.total_xof)

    db.add(order)
    await db.flush()
    return order


async def confirm_order(order: Order) -> None:
    order.confirm()


async def fulfill_order(order: Order) -> None:
    order.fulfill()


async def cancel_order(
    db: AsyncSession, order: Order, reason: str | None = None, fee_xof: float | None = None
) -> None:
    for item in order.items:
        product = await db.get(Product, item.product_id)
        if product and product.track_inventory and product.quantity_in_stock is not None:
            product.quantity_in_stock += item.quantity
    order.cancel(reason, fee_xof=fee_xof)


async def update_order_quantity(db: AsyncSession, order: Order, contact: Contact, new_quantity: int) -> None:
    """Customer-initiated quantity change on an order that hasn't shipped yet.
    Keeps the originally-quoted unit price (a promo that started/ended since
    the order was placed shouldn't retroactively change what was agreed),
    just recomputes stock, the item/order totals, and the contact's spend."""
    item = order.items[0]
    product = await db.get(Product, item.product_id)
    delta = new_quantity - item.quantity
    if product.track_inventory and product.quantity_in_stock is not None and delta > 0 and delta > product.quantity_in_stock:
        raise InsufficientStockError(f"Cette quantité n'est pas disponible pour {product.name}.")

    if product.track_inventory and product.quantity_in_stock is not None:
        product.quantity_in_stock -= delta

    old_item_total = float(item.total_price_xof)
    item.quantity = new_quantity
    item.total_price_xof = float(item.unit_price_xof) * new_quantity
    order.calculate_total()
    contact.total_spent_xof = float(contact.total_spent_xof or 0) + (float(item.total_price_xof) - old_item_total)


async def accept_return(order: Order) -> None:
    order.accept_return()


async def dismiss_return_request(order: Order) -> None:
    order.dismiss_return_request()
