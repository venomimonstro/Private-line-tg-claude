from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, Enum as SAEnum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.enums import AdminRole, UserStatus

if TYPE_CHECKING:
    from app.models.subscription import Subscription
    from app.models.payment import Payment
    from app.models.vpn_key import VpnKey
    from app.models.referral import Referral


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(UserStatus, values_callable=lambda x: [e.value for e in x]),
        default=UserStatus.new,
        nullable=False,
    )
    referrer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    admin_role: Mapped[AdminRole | None] = mapped_column(
        SAEnum(AdminRole, values_callable=lambda x: [e.value for e in x]),
        nullable=True,
    )
    admin_password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    admin_login_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    admin_locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rules_accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    subscriptions: Mapped[list[Subscription]] = relationship(
        "Subscription", back_populates="user", lazy="select"
    )
    payments: Mapped[list[Payment]] = relationship(
        "Payment", back_populates="user", lazy="select"
    )
    vpn_keys: Mapped[list[VpnKey]] = relationship(
        "VpnKey", back_populates="user", lazy="select"
    )
    referrals_sent: Mapped[list[Referral]] = relationship(
        "Referral",
        foreign_keys="[Referral.referrer_id]",
        back_populates="referrer",
        lazy="select",
    )
    referral_received: Mapped[Referral | None] = relationship(
        "Referral",
        foreign_keys="[Referral.invited_user_id]",
        back_populates="invited_user",
        lazy="select",
    )
