import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FAQ


async def match_faq(db: AsyncSession, business_id: uuid.UUID, message: str) -> FAQ | None:
    """Best keyword match, ported from the old MatchesMessage/CalculateMatchScore
    logic: score = count of the FAQ's keywords present in the message, highest
    score wins, ties broken by display order (first inserted)."""
    faqs = (
        await db.execute(select(FAQ).where(FAQ.business_id == business_id, FAQ.is_active == True))  # noqa: E712
    ).scalars().all()

    best: FAQ | None = None
    best_score = 0
    for faq in faqs:
        score = faq.match_score(message)
        if score > best_score:
            best = faq
            best_score = score
    return best
