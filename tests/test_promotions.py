from datetime import UTC, datetime, timedelta

from app.models import Business, Contact, DeliveryType, PaymentMethod, Product, Promotion
from app.services.orders.service import create_order, effective_price


async def _make_business_product_contact(db_session):
    business = Business(
        name="Test Boutique",
        whatsapp_number="221700000010",
        owner_whatsapp_number="221700000011",
    )
    db_session.add(business)
    await db_session.flush()

    product = Product(business_id=business.id, name="Riz 5kg", price_xof=4500, track_inventory=True, quantity_in_stock=10)
    db_session.add(product)

    contact = Contact(business_id=business.id, wa_id="221770001111", phone_number="221770001111")
    db_session.add(contact)
    await db_session.flush()
    return business, product, contact


def test_promotion_is_currently_active_within_window():
    promo = Promotion(
        title="Promo",
        discount_percent=10,
        end_date=datetime.now(UTC) + timedelta(days=1),
        is_active=True,
    )
    assert promo.is_currently_active() is True


def test_promotion_is_not_active_after_end_date():
    promo = Promotion(
        title="Promo",
        discount_percent=10,
        end_date=datetime.now(UTC) - timedelta(days=1),
        is_active=True,
    )
    assert promo.is_currently_active() is False


def test_promotion_is_not_active_when_flag_disabled():
    promo = Promotion(
        title="Promo",
        discount_percent=10,
        end_date=datetime.now(UTC) + timedelta(days=1),
        is_active=False,
    )
    assert promo.is_currently_active() is False


def test_calculate_discounted_price():
    promo = Promotion(title="Promo", discount_percent=10, end_date=datetime.now(UTC))
    assert promo.calculate_discounted_price(4500) == 4050.0


async def test_effective_price_applies_active_promotion(db_session):
    business, product, _ = await _make_business_product_contact(db_session)
    db_session.add(
        Promotion(
            business_id=business.id,
            product_id=product.id,
            title="Promo -10%",
            discount_percent=10,
            end_date=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await db_session.flush()

    price = await effective_price(db_session, product)
    assert price == 4050.0


async def test_effective_price_is_full_price_without_promotion(db_session):
    business, product, _ = await _make_business_product_contact(db_session)
    price = await effective_price(db_session, product)
    assert price == 4500.0


async def test_order_creation_applies_promotion_discount_to_order_item(db_session):
    business, product, contact = await _make_business_product_contact(db_session)
    db_session.add(
        Promotion(
            business_id=business.id,
            product_id=product.id,
            title="Promo -10%",
            discount_percent=10,
            end_date=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await db_session.flush()

    order = await create_order(
        db_session,
        business,
        contact,
        product,
        2,
        "Amadou Fall",
        DeliveryType.PICKUP,
        None,
        PaymentMethod.CASH,
    )

    assert float(order.items[0].unit_price_xof) == 4050.0
    assert float(order.total_xof) == 8100.0
    assert order.order_number.startswith("CMD-")
