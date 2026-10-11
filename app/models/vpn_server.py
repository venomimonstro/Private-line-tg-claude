from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.enums import VpnServerStatus


class VpnServer(Base):
    __tablename__ = "vpn_servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    country: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    api_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_secret_encrypted: Mapped[str] = mapped_column(String(1024), nullable=False)
    # SSH access is used only by the control-plane rollout orchestrator.
    # Secrets are Fernet-encrypted and are never rendered back into admin HTML.
    ssh_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ssh_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ssh_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ssh_auth_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ssh_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[VpnServerStatus] = mapped_column(
        SAEnum(VpnServerStatus, values_callable=lambda x: [e.value for e in x], native_enum=False),
        default=VpnServerStatus.active,
        nullable=False,
    )
    max_users: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    current_users: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    city: Mapped[str | None] = mapped_column(String(64), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(5), nullable=True)
    flag_emoji: Mapped[str | None] = mapped_column(String(10), nullable=True)
    is_selectable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    @property
    def ssh_configured(self) -> bool:
        return bool(
            self.ssh_host
            and self.ssh_port
            and self.ssh_username
            and self.ssh_auth_type in ("password", "private_key")
            and self.ssh_secret_encrypted
        )
