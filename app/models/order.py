import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import DeliveryType, OrderStatus, PaymentMethod


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("businesses.id"))
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("contacts.id"))

    # CMD-{YYYYMMDD}-{seq:04d} — the format already used in the Charte-validated
    # conversation scripts (the old build's code used a different "ORD-..."
    # format than its own prompt text; this rebuild picks one and sticks to it).
    order_number: Mapped[str] = mapped_column(String(20), unique=True)

    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status", native_enum=False), default=OrderStatus.PENDING
    )

    subtotal_xof: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    delivery_fee_xof: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    total_xof: Mapped[float] = mapped_column(Numeric(18, 2), default=0)

    delivery_type: Mapped[DeliveryType | None] = mapped_column(
        Enum(DeliveryType, name="delivery_type", native_enum=False)
    )
    delivery_address: Mapped[str | None] = mapped_column(String(500))
    customer_name: Mapped[str | None] = mapped_column(String(200))
    payment_method: Mapped[PaymentMethod] = mapped_column(
        Enum(PaymentMethod, name="payment_method", native_enum=False), default=PaymentMethod.CASH
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(String(500))

    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    business: Mapped["Business"] = relationship()  # noqa: F821
    contact: Mapped["Contact"] = relationship()  # noqa: F821

    def calculate_total(self) -> None:
        self.subtotal_xof = sum(float(item.total_price_xof) for item in self.items)
        self.total_xof = float(self.subtotal_xof) + float(self.delivery_fee_xof or 0)

    def confirm(self) -> None:
        self.status = OrderStatus.CONFIRMED
        self.confirmed_at = datetime.now(UTC)

    def fulfill(self) -> None:
        self.status = OrderStatus.FULFILLED
        self.fulfilled_at = datetime.now(UTC)

    def cancel(self, reason: str | None = None) -> None:
        self.status = OrderStatus.CANCELLED
        self.cancelled_at = datetime.now(UTC)
        self.cancellation_reason = reason

    @staticmethod
    def generate_order_number(today: datetime, daily_sequence: int) -> str:
        return f"CMD-{today:%Y%m%d}-{daily_sequence:04d}"


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"))
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"))

    product_name: Mapped[str] = mapped_column(String(200))  # snapshot at order time
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_xof: Mapped[float] = mapped_column(Numeric(18, 2))
    total_price_xof: Mapped[float] = mapped_column(Numeric(18, 2))

    order: Mapped["Order"] = relationship(back_populates="items")

    @staticmethod
    def from_product(product, quantity: int, unit_price_xof: float) -> "OrderItem":
        return OrderItem(
            product_id=product.id,
            product_name=product.name,
            quantity=quantity,
            unit_price_xof=unit_price_xof,
            total_price_xof=float(unit_price_xof) * quantity,
        )
