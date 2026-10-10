from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.enums import BackupType, UpdateStatus
from app.models.system_update import SystemUpdate
from app.services.notification import notify_admin
from backup_manager.backup import create_backup
from health_monitor.checker import run_health_check
from update_manager.preflight import run_preflight_checks, safe_extract_zip
from update_manager.rollback import perform_rollback

logger = logging.getLogger(__name__)


async def _set_update_status(update: SystemUpdate, status: UpdateStatus, session: AsyncSession) -> None:
    update.status = status
    await session.flush()
    await session.commit()


async def install_update(zip_path: str, admin_id: int, session: AsyncSession) -> SystemUpdate:
    manifest_path_in_zip = "manifest.json"

    with zipfile.ZipFile(zip_path, "r") as zf:
        with zf.open(manifest_path_in_zip) as f:
            manifest = json.load(f)

    new_version = manifest["version"]
    current_version = settings.APP_VERSION

    update = SystemUpdate(
        version_from=current_version,
        version_to=new_version,
        status=UpdateStatus.uploaded,
        started_at=datetime.utcnow(),
        initiated_by_admin_id=admin_id,
    )
    session.add(update)
    await session.flush()
    await session.commit()

    log_file = Path(settings.BACKUP_DIR) / f"update_{update.id}.log"
    update.log_path = str(log_file)

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.INFO)
    update_logger = logging.getLogger(f"update_{update.id}")
    update_logger.addHandler(file_handler)

    try:
        # Step 1: Preflight checks
        await _set_update_status(update, UpdateStatus.preflight_check, session)
        update_logger.info("Running preflight checks...")
        failures = await run_preflight_checks(zip_path, session)
        if failures:
            msg = "Preflight failed:\n" + "\n".join(failures)
            update_logger.error(msg)
            update.status = UpdateStatus.failed
            update.error_message = msg
            update.finished_at = datetime.utcnow()
            await session.commit()
            await notify_admin(f"❌ Обновление {new_version} не установлено.\n{msg}")
            return update

        # Step 2: Pre-update backup
        await _set_update_status(update, UpdateStatus.backup_created, session)
        update_logger.info("Creating pre-update backup...")
        backup = await create_backup(BackupType.pre_update, session, created_by=f"update_{update.id}")
        update.backup_id = backup.id
        await session.flush()
        await session.commit()

        # Step 3: Extract to temp directory
        await _set_update_status(update, UpdateStatus.installing, session)
        extract_dir = Path(tempfile.mkdtemp(prefix=f"pl_update_{update.id}_"))
        update_logger.info("Extracting ZIP to %s", extract_dir)

        safe_extract_zip(zip_path, extract_dir)

        # Step 4: Copy files to app dir
        files_dir = extract_dir / "files"
        if files_dir.exists():
            update_logger.info("Installing files...")
            for src in files_dir.rglob("*"):
                if src.is_file():
                    rel = src.relative_to(files_dir)
                    dst = Path(settings.APP_DIR) / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    update_logger.info("Installed: %s", rel)

        # Step 5: Run migrations
        migrations_dir = extract_dir / "migrations"
        if manifest.get("requires_db_migration"):
            raise RuntimeError(
                "In-process database migrations are disabled; use host-level release deployment"
            )
        if False and migrations_dir.exists():  # defensive dead branch; kept for old package compatibility
            await _set_update_status(update, UpdateStatus.migrating, session)
            update_logger.info("Running database migrations...")
            result = subprocess.run(
                ["alembic", "upgrade", "head"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=settings.APP_DIR,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Migration failed:\n{result.stderr}")
            update_logger.info("Migrations completed.")

        # Step 6: Health check
        await _set_update_status(update, UpdateStatus.health_check, session)
        update_logger.info("Running health check...")
        health = await run_health_check(session)
        if health.status.value in ("CRITICAL",):
            raise RuntimeError(f"Post-update health check failed: {health.status}")

        # Step 7: Success
        update.status = UpdateStatus.success
        update.finished_at = datetime.utcnow()
        await session.commit()

        update_logger.info("Update %s installed successfully.", new_version)
        await notify_admin(f"✅ Обновление до версии {new_version} установлено успешно.")
        shutil.rmtree(extract_dir, ignore_errors=True)
        return update

    except Exception as exc:
        logger.error("Update installation failed: %s", exc)
        update.error_message = str(exc)
        update.finished_at = datetime.utcnow()
        await session.flush()
        await session.commit()

        await perform_rollback(update.id, update.backup_id, session)
        return update
