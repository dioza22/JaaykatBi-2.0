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
        raise InsufficientStockError(
            f"Il ne reste que {product.quantity_in_stock} unité(s) de {product.name}."
        )

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


async def cancel_order(db: AsyncSession, order: Order, reason: str | None = None) -> None:
    for item in order.items:
        product = await db.get(Product, item.product_id)
        if product and product.track_inventory and product.quantity_in_stock is not None:
            product.quantity_in_stock += item.quantity
    order.cancel(reason)
