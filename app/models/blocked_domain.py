from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.enums import DomainStatus


class BlockedDomain(Base):
    __tablename__ = "blocked_domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(128), default="manual", nullable=False)
    status: Mapped[DomainStatus] = mapped_column(
        SAEnum(DomainStatus, values_callable=lambda x: [e.value for e in x]),
        default=DomainStatus.active,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
