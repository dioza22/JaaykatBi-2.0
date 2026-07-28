from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Business, Contact, DeliveryType, Order, PaymentMethod, Product, Promotion
from app.services.analytics.service import build_merchant_analytics
from app.services.orders.service import accept_return, cancel_order, confirm_order, create_order, fulfill_order

pytestmark = pytest.mark.asyncio


async def _setup(db_session):
    business = Business(
        name="Boutique Test",
        whatsapp_number="221700000050",
        owner_whatsapp_number="221700000051",
    )
    db_session.add(business)
    await db_session.flush()
    return business


def _new_customer(business, suffix):
    return Contact(business_id=business.id, wa_id=f"22177000{suffix}", phone_number=f"22177000{suffix}")


async def test_analytics_excludes_cancelled_and_returned_from_revenue_but_counts_status_breakdown(db_session):
    business = await _setup(db_session)
    product = Product(business_id=business.id, name="Riz", price_xof=1000)
    db_session.add(product)
    await db_session.flush()

    customer = _new_customer(business, "01")
    db_session.add(customer)
    await db_session.flush()

    fulfilled_order = await create_order(
        db_session, business, customer, product, 1, "Client", DeliveryType.PICKUP, None, PaymentMethod.CASH,
    )
    await fulfill_order(fulfilled_order)

    pending_order = await create_order(
        db_session, business, customer, product, 1, "Client", DeliveryType.PICKUP, None, PaymentMethod.CASH,
    )

    cancelled_order = await create_order(
        db_session, business, customer, product, 1, "Client", DeliveryType.PICKUP, None, PaymentMethod.CASH,
    )
    await cancel_order(db_session, cancelled_order, reason="test")

    returned_order = await create_order(
        db_session, business, customer, product, 1, "Client", DeliveryType.PICKUP, None, PaymentMethod.CASH,
    )
    await fulfill_order(returned_order)
    await accept_return(returned_order)
    await db_session.flush()

    analytics = await build_merchant_analytics(db_session, business)

    # only the fulfilled + pending orders count toward revenue (2 x 1000 FCFA)
    assert "Total depuis le début : 2 commande(s), 2000 FCFA." in analytics
    # but the status breakdown reflects every order regardless of resolution
    assert "pending : 1" in analytics
    assert "confirmed" not in analytics or "confirmed : 1" not in analytics  # none confirmed here
    assert "fulfilled : 1" in analytics
    assert "cancelled : 1" in analytics
    assert "returned : 1" in analytics


async def test_analytics_top_products_ranked_by_revenue(db_session):
    business = await _setup(db_session)
    cheap = Product(business_id=business.id, name="Sucre", price_xof=500)
    pricey = Product(business_id=business.id, name="Riz Premium", price_xof=5000)
    db_session.add_all([cheap, pricey])
    await db_session.flush()

    customer = _new_customer(business, "02")
    db_session.add(customer)
    await db_session.flush()

    # cheap product sold in bulk (5 x 500 = 2500) vs pricey sold once (5000) — pricey should rank first
    await create_order(db_session, business, customer, cheap, 5, "Client", DeliveryType.PICKUP, None, PaymentMethod.CASH)
    await create_order(db_session, business, customer, pricey, 1, "Client", DeliveryType.PICKUP, None, PaymentMethod.CASH)
    await db_session.flush()

    analytics = await build_merchant_analytics(db_session, business)

    top_line = next(line for line in analytics.splitlines() if "Meilleures ventes" in line)
    assert top_line.index("Riz Premium") < top_line.index("Sucre")
    assert "5000 FCFA, 1 unité(s)" in top_line
    assert "2500 FCFA, 5 unité(s)" in top_line


async def test_analytics_flags_never_sold_products_but_not_unavailable_ones(db_session):
    business = await _setup(db_session)
    sold = Product(business_id=business.id, name="Huile", price_xof=1500)
    never_sold = Product(business_id=business.id, name="Savon", price_xof=800)
    unavailable = Product(business_id=business.id, name="Ancien produit", price_xof=300, is_available=False)
    db_session.add_all([sold, never_sold, unavailable])
    await db_session.flush()

    customer = _new_customer(business, "03")
    db_session.add(customer)
    await db_session.flush()
    await create_order(db_session, business, customer, sold, 1, "Client", DeliveryType.PICKUP, None, PaymentMethod.CASH)
    await db_session.flush()

    analytics = await build_merchant_analytics(db_session, business)

    assert "Savon" in analytics
    assert "Huile" not in [
        p.strip() for line in analytics.splitlines() if "jamais vendus" in line for p in line.split(":")[-1].split(",")
    ]
    assert "Ancien produit" not in analytics  # unavailable — not a merchandising concern


async def test_analytics_flags_low_stock_products_only(db_session):
    business = await _setup(db_session)
    low = Product(
        business_id=business.id, name="Lait en poudre", price_xof=2000,
        track_inventory=True, quantity_in_stock=2, low_stock_threshold=5,
    )
    healthy = Product(
        business_id=business.id, name="Farine", price_xof=1200,
        track_inventory=True, quantity_in_stock=40, low_stock_threshold=5,
    )
    untracked = Product(business_id=business.id, name="Eau", price_xof=200, track_inventory=False)
    db_session.add_all([low, healthy, untracked])
    await db_session.flush()

    analytics = await build_merchant_analytics(db_session, business)

    assert "Lait en poudre (2 restant(s))" in analytics
    assert "Farine" not in [line for line in analytics.splitlines() if "stock faible" in line][0]


async def test_analytics_counts_repeat_customers(db_session):
    business = await _setup(db_session)
    product = Product(business_id=business.id, name="Riz", price_xof=1000)
    db_session.add(product)
    await db_session.flush()

    loyal = _new_customer(business, "04")
    one_timer = _new_customer(business, "05")
    db_session.add_all([loyal, one_timer])
    await db_session.flush()

    await create_order(db_session, business, loyal, product, 1, "Client", DeliveryType.PICKUP, None, PaymentMethod.CASH)
    await create_order(db_session, business, loyal, product, 1, "Client", DeliveryType.PICKUP, None, PaymentMethod.CASH)
    await create_order(db_session, business, one_timer, product, 1, "Client", DeliveryType.PICKUP, None, PaymentMethod.CASH)
    await db_session.flush()

    analytics = await build_merchant_analytics(db_session, business)
    assert "2 client(s) au total, dont 1 fidèle(s)" in analytics


async def test_analytics_lists_only_currently_active_promotions(db_session):
    business = await _setup(db_session)
    product = Product(business_id=business.id, name="Riz", price_xof=1000)
    db_session.add(product)
    await db_session.flush()

    active_promo = Promotion(
        business_id=business.id, product_id=product.id, title="Promo active",
        discount_percent=15, duration_days=7, end_date=datetime.now(UTC) + timedelta(days=3),
    )
    expired_promo = Promotion(
        business_id=business.id, product_id=product.id, title="Promo expirée",
        discount_percent=50, duration_days=7, end_date=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add_all([active_promo, expired_promo])
    await db_session.flush()

    analytics = await build_merchant_analytics(db_session, business)
    promo_line = next(line for line in analytics.splitlines() if "Promotions actives" in line)
    assert "Riz -15%" in promo_line
    assert "-50%" not in promo_line


async def test_analytics_reports_no_data_gracefully_for_a_brand_new_shop(db_session):
    business = await _setup(db_session)

    analytics = await build_merchant_analytics(db_session, business)
    assert "Aucune commande enregistrée à ce jour." in analytics
    assert "0 client(s) au total" in analytics
    assert "Aucune vente enregistrée" in analytics
    assert "Promotions actives : aucune" in analytics
