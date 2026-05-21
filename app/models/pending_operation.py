from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.subscription import Subscription
    from app.models.vpn_key import VpnKey
    from app.models.vpn_server import VpnServer


class PendingOperationType(str, Enum):
    create_key = "create_key"
    disable_key = "disable_key"
    delete_key = "delete_key"


class PendingOperation(Base):
    __tablename__ = "pending_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"), nullable=True)
    vpn_key_id: Mapped[int | None] = mapped_column(ForeignKey("vpn_keys.id"), nullable=True)
    server_id: Mapped[int | None] = mapped_column(ForeignKey("vpn_servers.id"), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    subscription: Mapped[Subscription | None] = relationship("Subscription")
    vpn_key: Mapped[VpnKey | None] = relationship("VpnKey")
    server: Mapped[VpnServer | None] = relationship("VpnServer")
