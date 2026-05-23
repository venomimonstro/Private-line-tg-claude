from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.enums import BackupStatus, BackupType
from app.models.backup import Backup

logger = logging.getLogger(__name__)

_BACKUP_RETENTION: dict[BackupType, int | None] = {
    BackupType.daily: 14,
    BackupType.pre_update: 10,
    BackupType.manual: None,
    BackupType.emergency: 14,
}


def _parse_pg_url(db_url: str) -> dict[str, str]:
    clean = db_url.split("?")[0]
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgres://"):
        clean = clean.replace(prefix, "postgresql://")
    parsed = urlparse(clean)
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "user": parsed.username or "",
        "password": parsed.password or "",
        "dbname": parsed.path.lstrip("/"),
    }


def _ensure_backup_dir() -> Path:
    path = Path(settings.BACKUP_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


async def create_backup(
    backup_type: BackupType,
    session: AsyncSession,
    created_by: str = "system",
) -> Backup:
    backup_dir = _ensure_backup_dir()
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{backup_type.value}_{timestamp}.db"
    file_path = backup_dir / filename

    record = Backup(
        type=backup_type,
        status=BackupStatus.creating,
        created_by=created_by,
    )
    session.add(record)
    await session.flush()

    try:
        db_url = settings.DATABASE_URL
        if "sqlite" in db_url:
            db_file = db_url.replace("sqlite+aiosqlite:///", "").replace("sqlite:///", "")
            db_file_path = Path(db_file)
            if not db_file_path.exists():
                raise FileNotFoundError(f"Database file not found: {db_file_path}")
            shutil.copy2(db_file_path, file_path)
        else:
            conn = _parse_pg_url(db_url)
            env = {**os.environ, "PGPASSWORD": conn["password"]}
            result = subprocess.run(
                [
                    "pg_dump", "-Fc",
                    "-h", conn["host"],
                    "-p", conn["port"],
                    "-U", conn["user"],
                    "-f", str(file_path),
                    conn["dbname"],
                ],
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
            )
            if result.returncode != 0:
                raise RuntimeError(f"pg_dump failed: {result.stderr}")

        checksum = _sha256_file(file_path)
        size_mb = file_path.stat().st_size / (1024 * 1024)

        record.status = BackupStatus.success
        record.file_path = str(file_path)
        record.checksum = checksum
        record.size_mb = round(size_mb, 3)
        await session.flush()

        logger.info("Backup created: %s (%.2f MB)", filename, size_mb)
        await _apply_retention_policy(backup_type, session)
        await session.commit()
        return record

    except Exception as exc:
        logger.error("Backup failed: %s", exc)
        record.status = BackupStatus.failed
        record.error_message = str(exc)
        await session.flush()
        await session.commit()
        raise


async def restore_backup(backup_id: int, session: AsyncSession) -> None:
    result = await session.execute(select(Backup).where(Backup.id == backup_id))
    backup = result.scalar_one_or_none()
    if backup is None:
        raise ValueError(f"Backup #{backup_id} not found")
    if backup.status != BackupStatus.success:
        raise ValueError(f"Backup #{backup_id} is not in success state")

    await create_backup(BackupType.emergency, session, created_by="pre_restore")

    file_path = Path(backup.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"Backup file not found: {file_path}")

    current_checksum = _sha256_file(file_path)
    if current_checksum != backup.checksum:
        raise ValueError("Backup file checksum mismatch — file may be corrupted")

    db_url = settings.DATABASE_URL
    if "sqlite" in db_url:
        db_file = db_url.replace("sqlite+aiosqlite:///", "").replace("sqlite:///", "")
        shutil.copy2(file_path, Path(db_file))
        logger.info("Database restored from backup #%d", backup_id)
    else:
        conn = _parse_pg_url(db_url)
        env = {**os.environ, "PGPASSWORD": conn["password"]}
        pg_args = ["-h", conn["host"], "-p", conn["port"], "-U", conn["user"]]
        subprocess.run(
            ["dropdb", "--if-exists", *pg_args, conn["dbname"]],
            capture_output=True, text=True, timeout=60, env=env,
        )
        subprocess.run(
            ["createdb", *pg_args, conn["dbname"]],
            capture_output=True, text=True, timeout=60, env=env,
        )
        result_restore = subprocess.run(
            ["pg_restore", "--no-owner", "--no-privileges",
             "-d", conn["dbname"], *pg_args, str(file_path)],
            capture_output=True, text=True, timeout=300, env=env,
        )
        if result_restore.returncode not in (0, 1):
            raise RuntimeError(f"pg_restore failed: {result_restore.stderr}")
        logger.info("PostgreSQL database restored from backup #%d", backup_id)


async def _apply_retention_policy(backup_type: BackupType, session: AsyncSession) -> None:
    max_count = _BACKUP_RETENTION.get(backup_type)
    if max_count is None:
        return

    result = await session.execute(
        select(Backup)
        .where(Backup.type == backup_type, Backup.status == BackupStatus.success)
        .order_by(Backup.created_at.desc())
    )
    all_backups = result.scalars().all()

    for old_backup in all_backups[max_count:]:
        if old_backup.file_path:
            try:
                Path(old_backup.file_path).unlink(missing_ok=True)
            except Exception as exc:
                logger.warning("Could not delete old backup file: %s", exc)
        await session.delete(old_backup)

    await session.flush()
