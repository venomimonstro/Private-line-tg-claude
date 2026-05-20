from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.enums import BackupStatus, BackupType


class Backup(Base):
    __tablename__ = "backups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[BackupType] = mapped_column(
        SAEnum(BackupType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    status: Mapped[BackupStatus] = mapped_column(
        SAEnum(BackupStatus, values_callable=lambda x: [e.value for e in x]),
        default=BackupStatus.creating,
        nullable=False,
    )
    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    remote_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), default="system", nullable=False)
    restore_test_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
