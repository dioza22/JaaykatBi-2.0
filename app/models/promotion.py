import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Promotion(Base):
    __tablename__ = "promotions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("businesses.id"))
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"))

    title: Mapped[str] = mapped_column(String(200))
    discount_percent: Mapped[int] = mapped_column(Integer)
    duration_days: Mapped[int] = mapped_column(Integer, default=7)  # 3/7/14 per merchant script

    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    business: Mapped["Business"] = relationship(back_populates="promotions")  # noqa: F821
    product: Mapped["Product"] = relationship()  # noqa: F821

    def is_currently_active(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        end = self.end_date if self.end_date.tzinfo else self.end_date.replace(tzinfo=UTC)
        return self.is_active and now <= end

    def calculate_discounted_price(self, price_xof: float) -> float:
        return round(float(price_xof) * (1 - self.discount_percent / 100), 2)
