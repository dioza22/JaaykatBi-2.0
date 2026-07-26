"""Conversation.state helpers. Unlike the old build's dead `Context` column,
this is actually read and written every turn — it's what makes order-taking
and merchant-admin flows deterministic instead of hoping the LLM remembers
which step it's on.

Always *reassign* `conversation.state` to a brand-new dict rather than
mutating the existing one in place — SQLAlchemy only detects attribute
reassignment on a plain JSONB column, not in-place mutation of the dict it
returned."""

from typing import Any


def _carry_over(conversation) -> dict[str, Any]:
    st = conversation.state or {}
    return {"needs_human": st.get("needs_human", False), "unknown_streak": st.get("unknown_streak", 0)}


def start_flow(conversation, flow: str, slots: dict[str, Any] | None = None) -> None:
    conversation.state = {"flow": flow, "step": 0, "slots": slots or {}, **_carry_over(conversation)}


def advance(conversation, step: int, slots: dict[str, Any]) -> None:
    flow = (conversation.state or {}).get("flow")
    conversation.state = {"flow": flow, "step": step, "slots": slots, **_carry_over(conversation)}


def clear_flow(conversation) -> None:
    conversation.state = {"flow": None, "step": 0, "slots": {}, **_carry_over(conversation)}


def set_needs_human(conversation, value: bool) -> None:
    st = dict(conversation.state or {})
    st["needs_human"] = value
    conversation.state = st


def increment_unknown_streak(conversation) -> int:
    st = dict(conversation.state or {})
    st["unknown_streak"] = st.get("unknown_streak", 0) + 1
    conversation.state = st
    return st["unknown_streak"]


def reset_unknown_streak(conversation) -> None:
    st = dict(conversation.state or {})
    if st.get("unknown_streak"):
        st["unknown_streak"] = 0
        conversation.state = st


def current_flow(conversation) -> str | None:
    return (conversation.state or {}).get("flow")


def current_step(conversation) -> int:
    return (conversation.state or {}).get("step", 0)


def current_slots(conversation) -> dict[str, Any]:
    return dict((conversation.state or {}).get("slots", {}))


def needs_human(conversation) -> bool:
    return bool((conversation.state or {}).get("needs_human", False))
