from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Release(Base):
    """Tracks every code deployment with its git state and outcome."""

    __tablename__ = "releases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[str] = mapped_column(String(80), nullable=False)
    commit_hash: Mapped[str | None] = mapped_column(String(40), nullable=True)
    previous_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # pending → validating → deploying → success / failed / rolled_back
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    changelog: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    deploy_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    backup_id: Mapped[int | None] = mapped_column(ForeignKey("backups.id"), nullable=True)
    triggered_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
