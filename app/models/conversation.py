import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import ConversationStatus


class Conversation(Base):
    """Unlike the old build, `state` here is actually read and written by the
    conversation engine — it's what drives the deterministic order-taking and
    merchant-admin flows (finite-state machine), not just a dead JSON column."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("businesses.id"))
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("contacts.id"))

    status: Mapped[ConversationStatus] = mapped_column(
        Enum(ConversationStatus, name="conversation_status", native_enum=False),
        default=ConversationStatus.ACTIVE,
    )

    # {"flow": "commander_produit" | "ajouter_produit" | ... | None,
    #  "step": int, "slots": {...}, "needs_human": bool}
    state: Mapped[dict] = mapped_column(JSONB, default=dict)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_count: Mapped[int] = mapped_column(Integer, default=0)

    business: Mapped["Business"] = relationship()  # noqa: F821
    contact: Mapped["Contact"] = relationship()  # noqa: F821

    def is_free_window_open(self) -> bool:
        """WhatsApp's 24h customer-service free-messaging window."""
        if self.last_message_at is None:
            return False
        last = self.last_message_at if self.last_message_at.tzinfo else self.last_message_at.replace(tzinfo=UTC)
        return (datetime.now(UTC) - last).total_seconds() < 24 * 3600
