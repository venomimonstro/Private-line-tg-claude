from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.enums import PaymentStatus

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.subscription import Subscription


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"), nullable=True)
    tariff_id: Mapped[int] = mapped_column(ForeignKey("tariffs.id"), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="RUB", nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(PaymentStatus, values_callable=lambda x: [e.value for e in x]),
        default=PaymentStatus.pending,
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(64), default="manual", nullable=False)
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    user: Mapped[User] = relationship("User", foreign_keys=[user_id], back_populates="payments")
    confirmed_by: Mapped[User | None] = relationship("User", foreign_keys=[confirmed_by_id])
    subscription: Mapped[Subscription | None] = relationship("Subscription", foreign_keys=[subscription_id])
