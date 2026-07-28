"""Real, DB-sourced business figures for the merchant-facing AI. The AI is
meant to act as an expert marketer/data analyst on top of being a shop
manager — but every insight it gives has to trace back to an actual number
computed here, never a guess. This module's only job is to compute those
numbers; app/services/ai/prompts.py turns them into prose for the LLM."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, Contact, Order, OrderItem, OrderStatus, Product, Promotion

_EXCLUDED_FROM_REVENUE = (OrderStatus.CANCELLED, OrderStatus.RETURNED)


async def _orders_count_and_revenue(
    db: AsyncSession, business_id, since: datetime | None = None
) -> tuple[int, float]:
    stmt = select(func.count(), func.coalesce(func.sum(Order.total_xof), 0)).where(
        Order.business_id == business_id, Order.status.not_in(_EXCLUDED_FROM_REVENUE)
    )
    if since is not None:
        stmt = stmt.where(Order.created_at >= since)
    row = (await db.execute(stmt)).one()
    return row[0], float(row[1])


async def _top_products_by_revenue(
    db: AsyncSession, business_id, limit: int = 3
) -> list[tuple[str, float, int]]:
    stmt = (
        select(
            OrderItem.product_name,
            func.sum(OrderItem.total_price_xof).label("revenue"),
            func.sum(OrderItem.quantity).label("qty"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.business_id == business_id, Order.status.not_in(_EXCLUDED_FROM_REVENUE))
        .group_by(OrderItem.product_name)
        .order_by(func.sum(OrderItem.total_price_xof).desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [(name, float(revenue), int(qty)) for name, revenue, qty in rows]


async def _never_sold_products(db: AsyncSession, business_id) -> list[str]:
    sold_product_ids = select(OrderItem.product_id).join(Order, Order.id == OrderItem.order_id).where(
        Order.business_id == business_id, Order.status.not_in(_EXCLUDED_FROM_REVENUE)
    )
    stmt = select(Product.name).where(
        Product.business_id == business_id,
        Product.is_available == True,  # noqa: E712
        Product.id.not_in(sold_product_ids),
    )
    return (await db.execute(stmt)).scalars().all()


async def _low_stock_products(db: AsyncSession, business_id) -> list[tuple[str, int]]:
    products = (
        await db.execute(
            select(Product).where(Product.business_id == business_id, Product.track_inventory == True)  # noqa: E712
        )
    ).scalars().all()
    return [(p.name, p.quantity_in_stock) for p in products if p.is_low_stock()]


async def _customer_loyalty(db: AsyncSession, business_id) -> tuple[int, int]:
    contacts = (await db.execute(select(Contact).where(Contact.business_id == business_id))).scalars().all()
    repeat_customers = sum(1 for c in contacts if c.total_orders >= 2)
    return len(contacts), repeat_customers


async def _order_status_breakdown(db: AsyncSession, business_id) -> dict[OrderStatus, int]:
    stmt = select(Order.status, func.count()).where(Order.business_id == business_id).group_by(Order.status)
    return {status: count for status, count in (await db.execute(stmt)).all()}


async def _active_promotions_summary(db: AsyncSession, business_id, now: datetime) -> list[str]:
    promotions = (
        await db.execute(
            select(Promotion).where(Promotion.business_id == business_id, Promotion.is_active == True)  # noqa: E712
        )
    ).scalars().all()
    lines = []
    for promo in promotions:
        if not promo.is_currently_active(now):
            continue
        product = await db.get(Product, promo.product_id)
        end = promo.end_date if promo.end_date.tzinfo else promo.end_date.replace(tzinfo=UTC)
        days_left = max((end - now).days, 0)
        lines.append(f"{product.name if product else '?'} -{promo.discount_percent}% ({days_left} jour(s) restant(s))")
    return lines


async def build_merchant_analytics(db: AsyncSession, business: Business) -> str:
    """Returns a plain-text, fact-only block: every line is a number pulled
    straight from a query above. Fed verbatim into the merchant system
    prompt so the LLM has real data to reason over instead of guessing."""
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    last_7_days = now - timedelta(days=7)
    last_30_days = now - timedelta(days=30)
    prior_30_days = now - timedelta(days=60)

    today_count, today_revenue = await _orders_count_and_revenue(db, business.id, today_start)
    week_count, week_revenue = await _orders_count_and_revenue(db, business.id, last_7_days)
    month_count, month_revenue = await _orders_count_and_revenue(db, business.id, last_30_days)
    prior_month_count, prior_month_revenue = await _orders_count_and_revenue(db, business.id, prior_30_days)
    # "prior 30 days" window is [60d ago, 30d ago) — the cumulative-since-60d
    # figure minus the cumulative-since-30d figure isolates that window.
    prior_month_only_revenue = prior_month_revenue - month_revenue
    prior_month_only_count = prior_month_count - month_count

    all_time_count, all_time_revenue = await _orders_count_and_revenue(db, business.id)
    average_order_value = (all_time_revenue / all_time_count) if all_time_count else 0.0

    top_products = await _top_products_by_revenue(db, business.id)
    never_sold = await _never_sold_products(db, business.id)
    low_stock = await _low_stock_products(db, business.id)
    total_customers, repeat_customers = await _customer_loyalty(db, business.id)
    status_breakdown = await _order_status_breakdown(db, business.id)
    active_promotions = await _active_promotions_summary(db, business.id, now)

    lines = [
        f"Aujourd'hui : {today_count} commande(s), {int(today_revenue)} FCFA.",
        f"7 derniers jours : {week_count} commande(s), {int(week_revenue)} FCFA.",
        f"30 derniers jours : {month_count} commande(s), {int(month_revenue)} FCFA.",
    ]

    if prior_month_only_revenue > 0:
        growth_pct = ((month_revenue - prior_month_only_revenue) / prior_month_only_revenue) * 100
        trend = "hausse" if growth_pct >= 0 else "baisse"
        lines.append(
            f"30 jours précédents (30-60j) : {prior_month_only_count} commande(s), "
            f"{int(prior_month_only_revenue)} FCFA — {trend} de {abs(growth_pct):.0f}% sur les 30 derniers jours."
        )
    else:
        lines.append("Pas assez d'historique (30-60j) pour calculer une tendance de croissance.")

    lines.append(f"Total depuis le début : {all_time_count} commande(s), {int(all_time_revenue)} FCFA.")
    lines.append(f"Panier moyen : {int(average_order_value)} FCFA.")

    if status_breakdown:
        lines.append(
            "Répartition des statuts : "
            + ", ".join(f"{status.value} : {count}" for status, count in status_breakdown.items())
        )
    else:
        lines.append("Aucune commande enregistrée à ce jour.")

    lines.append(
        f"Clientèle : {total_customers} client(s) au total, dont {repeat_customers} fidèle(s) "
        f"(2 commandes ou plus)."
    )

    if top_products:
        lines.append(
            "Meilleures ventes (par chiffre d'affaires) : "
            + "; ".join(f"{name} — {int(revenue)} FCFA, {qty} unité(s)" for name, revenue, qty in top_products)
        )
    else:
        lines.append("Aucune vente enregistrée — pas de classement des meilleures ventes possible.")

    if never_sold:
        lines.append("Produits disponibles jamais vendus : " + ", ".join(never_sold))

    if low_stock:
        lines.append(
            "Alerte stock faible : " + ", ".join(f"{name} ({qty} restant(s))" for name, qty in low_stock)
        )

    lines.append("Promotions actives : " + ("; ".join(active_promotions) if active_promotions else "aucune"))

    return "\n".join(f"- {line}" for line in lines)
