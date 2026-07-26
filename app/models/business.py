import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Business(Base):
    """A single merchant shop. v1 seeds one row (Boutique Teranga) but the
    schema supports more than one — nothing here assumes single-tenancy."""

    __tablename__ = "businesses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    owner_name: Mapped[str | None] = mapped_column(String(200))

    # The bot's own WhatsApp Cloud API number — customers message this number.
    whatsapp_number: Mapped[str] = mapped_column(String(20))

    # The merchant's personal WhatsApp number. A business can't message itself
    # on WhatsApp, so merchant admin commands are routed by checking whether
    # the inbound sender is this number rather than the bot's own number.
    owner_whatsapp_number: Mapped[str] = mapped_column(String(20))

    address: Mapped[str | None] = mapped_column(String(500))
    welcome_message: Mapped[str | None] = mapped_column(String(1000))
    away_message: Mapped[str | None] = mapped_column(String(1000))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    products: Mapped[list["Product"]] = relationship(back_populates="business")  # noqa: F821
    contacts: Mapped[list["Contact"]] = relationship(back_populates="business")  # noqa: F821
    faqs: Mapped[list["FAQ"]] = relationship(back_populates="business")  # noqa: F821
    promotions: Mapped[list["Promotion"]] = relationship(back_populates="business")  # noqa: F821
