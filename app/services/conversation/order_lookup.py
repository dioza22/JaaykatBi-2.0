"""Shared "which order did they mean" resolver — mirrors product_lookup.py.
Used by both the merchant order-management flow and the customer
return-request flow."""

from app.models import Order


def resolve_order(text: str, orders: list[Order]) -> Order | None:
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
