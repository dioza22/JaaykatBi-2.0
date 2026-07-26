"""Shared "which product did they mean" resolver — used by both the
customer order-taking flow and the merchant product-management flows."""

from app.models import Product


def resolve_product(text: str, products: list[Product]) -> Product | None:
    stripped = text.strip()
    if stripped.isdigit():
        idx = int(stripped)
        if 1 <= idx <= len(products):
            return products[idx - 1]
    text_lower = stripped.lower()
    for product in products:
        if product.name.lower() in text_lower or text_lower in product.name.lower():
            return product
    return None
