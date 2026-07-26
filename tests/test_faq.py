import pytest

from app.models import FAQ, Business
from app.services.faq.service import match_faq

pytestmark = pytest.mark.asyncio


async def _make_business(db_session) -> Business:
    business = Business(
        name="Test Boutique",
        whatsapp_number="221700000001",
        owner_whatsapp_number="221700000002",
    )
    db_session.add(business)
    await db_session.flush()
    return business


async def test_match_faq_picks_highest_scoring_keyword_match(db_session):
    business = await _make_business(db_session)
    db_session.add_all(
        [
            FAQ(
                business_id=business.id,
                question="Horaires ?",
                answer="8h-20h du lundi au samedi.",
                keywords="horaire,heure,ouverture",
            ),
            FAQ(
                business_id=business.id,
                question="Livraison ?",
                answer="Oui, nous livrons dans tout Dakar.",
                keywords="livraison,livrer,frais",
            ),
        ]
    )
    await db_session.flush()

    result = await match_faq(db_session, business.id, "Quels sont vos frais de livraison ?")
    assert result is not None
    assert "livrons" in result.answer


async def test_match_faq_returns_none_when_no_keyword_matches(db_session):
    business = await _make_business(db_session)
    db_session.add(
        FAQ(business_id=business.id, question="Horaires ?", answer="8h-20h.", keywords="horaire,heure")
    )
    await db_session.flush()

    result = await match_faq(db_session, business.id, "Bonjour comment allez-vous")
    assert result is None


async def test_match_faq_ignores_inactive_faqs(db_session):
    business = await _make_business(db_session)
    db_session.add(
        FAQ(
            business_id=business.id,
            question="Horaires ?",
            answer="8h-20h.",
            keywords="horaire",
            is_active=False,
        )
    )
    await db_session.flush()

    result = await match_faq(db_session, business.id, "horaire")
    assert result is None
