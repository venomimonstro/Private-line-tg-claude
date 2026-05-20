from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.enums import AbuseAction, AbuseSeverity


class AbuseEvent(Base):
    __tablename__ = "abuse_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    vpn_key_id: Mapped[int | None] = mapped_column(ForeignKey("vpn_keys.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[AbuseSeverity] = mapped_column(
        SAEnum(AbuseSeverity, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    target_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_taken: Mapped[AbuseAction] = mapped_column(
        SAEnum(AbuseAction, values_callable=lambda x: [e.value for e in x]),
        default=AbuseAction.log_only,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    admin_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
