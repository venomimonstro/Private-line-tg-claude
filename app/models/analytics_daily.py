from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AnalyticsDaily(Base):
    __tablename__ = "analytics_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, unique=True, nullable=False, index=True)
    new_users: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_subscriptions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    paid_orders: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revenue: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    renewals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expired_subscriptions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    traffic_total_gb: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    abuse_events_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
