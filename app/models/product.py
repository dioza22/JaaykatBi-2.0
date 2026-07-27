import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Product(Base):
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("businesses.id"))

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(1000))
    category: Mapped[str | None] = mapped_column(String(100))
    price_xof: Mapped[float] = mapped_column(Numeric(18, 2))

    track_inventory: Mapped[bool] = mapped_column(Boolean, default=False)
    quantity_in_stock: Mapped[int | None] = mapped_column(Integer)
    # Baseline set whenever the merchant explicitly sets a stock number (initial
    # add, or a later restock) — never touched by order fulfillment, so
    # quantity_in_stock/initial_stock reads as "X left of the Y last stocked."
    initial_stock: Mapped[int | None] = mapped_column(Integer)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=5)

    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())

    business: Mapped["Business"] = relationship(back_populates="products")  # noqa: F821

    def is_in_stock(self) -> bool:
        if not self.track_inventory:
            return True
        return (self.quantity_in_stock or 0) > 0

    def is_low_stock(self) -> bool:
        if not self.track_inventory or self.quantity_in_stock is None:
            return False
        return self.quantity_in_stock <= self.low_stock_threshold

    def deduct_stock(self, quantity: int) -> None:
        if self.track_inventory and self.quantity_in_stock is not None:
            self.quantity_in_stock = max(0, self.quantity_in_stock - quantity)
