import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Contact(Base):
    """A customer, scoped to one business (a person messaging two different
    shops is two separate Contact rows — matches how WhatsApp businesses
    actually see customers, one per phone number)."""

    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("businesses.id"))

    wa_id: Mapped[str] = mapped_column(String(50))
    phone_number: Mapped[str] = mapped_column(String(20))
    display_name: Mapped[str | None] = mapped_column(String(200))

    first_contact_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    total_orders: Mapped[int] = mapped_column(Integer, default=0)
    total_spent_xof: Mapped[float] = mapped_column(Numeric(18, 2), default=0)

    business: Mapped["Business"] = relationship(back_populates="contacts")  # noqa: F821

    def update_last_contact(self) -> None:
        self.last_contact_at = datetime.now(UTC)

    def increment_order_stats(self, amount_xof: float) -> None:
        self.total_orders += 1
        self.total_spent_xof = float(self.total_spent_xof or 0) + float(amount_xof)
