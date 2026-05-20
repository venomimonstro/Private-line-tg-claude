from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.enums import SubscriptionStatus

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.vpn_key import VpnKey


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    tariff_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[SubscriptionStatus] = mapped_column(
        SAEnum(SubscriptionStatus, values_callable=lambda x: [e.value for e in x]),
        default=SubscriptionStatus.pending_payment,
        nullable=False,
        index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    traffic_limit_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    devices_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    user: Mapped[User] = relationship("User", back_populates="subscriptions")
    vpn_keys: Mapped[list[VpnKey]] = relationship("VpnKey", back_populates="subscription", lazy="select")
