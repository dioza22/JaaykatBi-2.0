"""Routes an inbound message to a deterministic flow, an FAQ answer, or the
LLM fallback. The FSM (state.py + customer_flows.py/merchant_flows.py) drives
order-taking and merchant admin; the LLM is only ever consulted for
open-ended catalog Q&A / general chat, never for step tracking."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, Contact, Conversation, MessageDirection
from app.models import Message as MessageModel
from app.services.ai.llm_client import LLMClient
from app.services.ai.prompts import build_system_prompt
from app.services.conversation import customer_flows, merchant_flows, state
from app.services.conversation.intents import Intent, detect_intent
from app.services.faq.service import match_faq
from app.services.orders.service import cancel_order
from app.models import Order, OrderStatus

_UNKNOWN_STREAK_BEFORE_HANDOFF = 3

_llm_client = LLMClient()


async def _message_history(db: AsyncSession, conversation_id, limit: int = 10) -> list[tuple[str, str]]:
    rows = (
        await db.execute(
            select(MessageModel)
            .where(MessageModel.conversation_id == conversation_id)
            .order_by(MessageModel.sent_at.desc())
            .limit(limit + 1)
        )
    ).scalars().all()
    if not rows:
        return []
    history_rows = list(reversed(rows[1:]))  # rows[0] is the just-persisted current inbound message
    return [
        ("user" if m.direction == MessageDirection.INBOUND else "model", m.content) for m in history_rows
    ]


async def _customer_order_status(db: AsyncSession, business: Business, contact: Contact) -> str:
    order = await db.scalar(
        select(Order)
        .where(Order.business_id == business.id, Order.contact_id == contact.id)
        .order_by(Order.created_at.desc())
    )
    if order is None:
        return "Vous n'avez pas encore de commande chez nous. Répondez 'catalogue' pour découvrir nos produits."
    status_labels = {
        OrderStatus.PENDING: "en attente de confirmation",
        OrderStatus.CONFIRMED: "confirmée, en préparation",
        OrderStatus.FULFILLED: "livrée/récupérée",
        OrderStatus.CANCELLED: "annulée",
    }
    return f"Votre commande {order.order_number} est {status_labels[order.status]}."


async def _customer_cancel_order(db: AsyncSession, business: Business, contact: Contact) -> str:
    order = await db.scalar(
        select(Order)
        .where(
            Order.business_id == business.id,
            Order.contact_id == contact.id,
            Order.status == OrderStatus.PENDING,
        )
        .order_by(Order.created_at.desc())
    )
    if order is None:
        return (
            "Je ne trouve pas de commande annulable (déjà confirmée ou aucune commande en cours). "
            "Contactez-nous directement si besoin."
        )
    await cancel_order(db, order, reason="Annulée par le client via WhatsApp")
    return f"Votre commande {order.order_number} a été annulée. -- Jaaykat bi"


async def _handoff_message(business: Business) -> str:
    return (
        f"Je transmets votre message à {business.owner_name or 'notre équipe'}, qui vous répondra bientôt. "
        "Merci de votre patience. -- Jaaykat bi"
    )


async def handle_message(
    db: AsyncSession,
    business: Business,
    contact: Contact,
    conversation: Conversation,
    text: str,
    is_merchant: bool,
) -> str:
    active_flow = state.current_flow(conversation)

    # A flow-wide "cancel" escape hatch, checked before delegating to whichever
    # flow (customer or merchant) is currently in progress.
    if active_flow and text.strip().lower() in {"annuler", "annuler commande", "stop"}:
        state.clear_flow(conversation)
        return "D'accord, j'ai annulé l'opération en cours. -- Jaaykat bi"

    if active_flow:
        if is_merchant:
            return await merchant_flows.continue_flow(db, business, conversation, text)
        return await customer_flows.continue_order_flow(db, business, contact, conversation, text)

    if is_merchant:
        return await merchant_flows.handle_intent(db, business, conversation, text)

    intent = detect_intent(text, is_merchant=False)

    if intent == Intent.GREETING:
        state.reset_unknown_streak(conversation)
        return business.welcome_message or "Bonjour ! Comment puis-je vous aider ? -- Jaaykat bi"

    if intent == Intent.GOODBYE:
        state.reset_unknown_streak(conversation)
        return "Merci pour votre visite. Au plaisir de vous revoir bientôt. -- Jaaykat bi"

    if intent == Intent.VOIR_CATALOGUE:
        state.reset_unknown_streak(conversation)
        return await customer_flows.catalog_message(db, business)

    if intent == Intent.VOIR_PROMOTIONS:
        state.reset_unknown_streak(conversation)
        return await customer_flows.promotions_message(db, business)

    if intent == Intent.COMMANDER_PRODUIT:
        state.reset_unknown_streak(conversation)
        return await customer_flows.start_order_flow(db, business, conversation)

    if intent == Intent.STATUT_COMMANDE:
        state.reset_unknown_streak(conversation)
        return await _customer_order_status(db, business, contact)

    if intent == Intent.ANNULER_COMMANDE:
        state.reset_unknown_streak(conversation)
        return await _customer_cancel_order(db, business, contact)

    if intent == Intent.PARLER_A_QUELQUUN:
        state.set_needs_human(conversation, True)
        return await _handoff_message(business)

    # UNKNOWN: try an FAQ match first (cheap, no LLM call), then fall back to
    # the LLM for open-ended catalog Q&A / general chat.
    faq = await match_faq(db, business.id, text)
    if faq:
        state.reset_unknown_streak(conversation)
        return faq.answer

    products = await customer_flows.available_products(db, business.id)
    system_prompt = build_system_prompt(business, products)
    history = await _message_history(db, conversation.id)
    reply = await _llm_client.generate(system_prompt, history, text)

    if reply is None:
        streak = state.increment_unknown_streak(conversation)
        if streak >= _UNKNOWN_STREAK_BEFORE_HANDOFF:
            state.set_needs_human(conversation, True)
            return await _handoff_message(business)
        return "Je n'ai pas bien compris. Pourriez-vous reformuler, ou répondre 'catalogue' pour voir nos produits ?"

    state.reset_unknown_streak(conversation)
    return reply
