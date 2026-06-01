from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SubscriptionNotification(Base):
    __tablename__ = "subscription_notifications"
    __table_args__ = (UniqueConstraint("subscription_id", "notification_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), nullable=False, index=True)
    notification_type: Mapped[str] = mapped_column(String(20), nullable=False)  # 24h, 12h, expiry, post24h
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
