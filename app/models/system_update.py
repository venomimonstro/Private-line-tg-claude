from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.enums import UpdateStatus


class SystemUpdate(Base):
    __tablename__ = "system_updates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_from: Mapped[str] = mapped_column(String(64), nullable=False)
    version_to: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[UpdateStatus] = mapped_column(
        SAEnum(UpdateStatus, values_callable=lambda x: [e.value for e in x], native_enum=False),
        default=UpdateStatus.uploaded,
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    backup_id: Mapped[int | None] = mapped_column(ForeignKey("backups.id"), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    initiated_by_admin_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
