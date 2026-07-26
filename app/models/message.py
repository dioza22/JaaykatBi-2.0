import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import MessageDirection, MessageStatus, MessageType


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id"))

    direction: Mapped[MessageDirection] = mapped_column(
        Enum(MessageDirection, name="message_direction", native_enum=False)
    )
    message_type: Mapped[MessageType] = mapped_column(
        Enum(MessageType, name="message_type", native_enum=False), default=MessageType.TEXT
    )
    content: Mapped[str] = mapped_column(String(4000))

    whatsapp_message_id: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus, name="message_status", native_enum=False), default=MessageStatus.PENDING
    )

    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    was_ai_generated: Mapped[bool] = mapped_column(Boolean, default=False)

    conversation: Mapped["Conversation"] = relationship()  # noqa: F821
