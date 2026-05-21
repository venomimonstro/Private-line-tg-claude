from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum as SAEnum, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.enums import VpnKeyStatus

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.subscription import Subscription
    from app.models.vpn_server import VpnServer


class VpnKey(Base):
    __tablename__ = "vpn_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), nullable=False, index=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("vpn_servers.id"), nullable=False)
    outline_key_id: Mapped[str] = mapped_column(String(255), nullable=False)
    access_url_encrypted: Mapped[str] = mapped_column(String(2048), nullable=False)
    status: Mapped[VpnKeyStatus] = mapped_column(
        SAEnum(VpnKeyStatus, values_callable=lambda x: [e.value for e in x]),
        default=VpnKeyStatus.active,
        nullable=False,
        index=True,
    )
    traffic_used_gb: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    traffic_limit_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_reissued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship("User", back_populates="vpn_keys")
    subscription: Mapped[Subscription] = relationship("Subscription", back_populates="vpn_keys")
    server: Mapped[VpnServer] = relationship("VpnServer")
