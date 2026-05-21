from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.enums import ReferralStatus

if TYPE_CHECKING:
    from app.models.user import User


class Referral(Base):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    invited_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, unique=True, index=True)
    bonus_days_awarded: Mapped[int] = mapped_column(Integer, default=14, nullable=False)
    status: Mapped[ReferralStatus] = mapped_column(
        SAEnum(ReferralStatus, values_callable=lambda x: [e.value for e in x]),
        default=ReferralStatus.pending,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    referrer: Mapped[User] = relationship(
        "User", foreign_keys=[referrer_id], back_populates="referrals_sent"
    )
    invited_user: Mapped[User] = relationship(
        "User", foreign_keys=[invited_user_id], back_populates="referral_received"
    )
