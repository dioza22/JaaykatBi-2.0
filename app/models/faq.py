import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class FAQ(Base):
    __tablename__ = "faqs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("businesses.id"))

    question: Mapped[str] = mapped_column(String(500))
    answer: Mapped[str] = mapped_column(String(2000))
    keywords: Mapped[str | None] = mapped_column(String(500))  # comma-separated
    category: Mapped[str | None] = mapped_column(String(100))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    business: Mapped["Business"] = relationship(back_populates="faqs")  # noqa: F821

    def keyword_list(self) -> list[str]:
        if not self.keywords:
            return []
        return [k.strip().lower() for k in self.keywords.split(",") if k.strip()]

    def match_score(self, message: str) -> int:
        message_lower = message.lower()
        return sum(1 for kw in self.keyword_list() if kw in message_lower)
