from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, Float, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.enums import HealthStatus


class SystemHealthCheck(Base):
    __tablename__ = "system_health_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[HealthStatus] = mapped_column(
        SAEnum(HealthStatus, values_callable=lambda x: [e.value for e in x], native_enum=False),
        nullable=False,
    )
    bot_status: Mapped[str] = mapped_column(String(16), nullable=False)
    backend_status: Mapped[str] = mapped_column(String(16), nullable=False)
    database_status: Mapped[str] = mapped_column(String(16), nullable=False)
    vpn_api_status: Mapped[str] = mapped_column(String(16), nullable=False)
    dns_filter_status: Mapped[str] = mapped_column(String(16), nullable=False)
    firewall_status: Mapped[str] = mapped_column(String(16), nullable=False)
    backup_status: Mapped[str] = mapped_column(String(16), nullable=False)
    free_disk_space: Mapped[float | None] = mapped_column(Float, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
