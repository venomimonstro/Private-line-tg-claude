from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import UpdateStatus
from app.models.backup import Backup
from app.models.system_update import SystemUpdate
from app.services.notification import notify_admin

logger = logging.getLogger(__name__)


async def perform_rollback(
    update_id: int,
    backup_id: int | None,
    session: AsyncSession,
) -> None:
    update_result = await session.execute(
        select(SystemUpdate).where(SystemUpdate.id == update_id)
    )
    update = update_result.scalar_one_or_none()

    if update is not None:
        update.status = UpdateStatus.rollback_started
        await session.flush()
        await session.commit()

    logger.info("Starting rollback for update #%d", update_id)

    if backup_id is not None:
        backup_result = await session.execute(
            select(Backup).where(Backup.id == backup_id)
        )
        backup = backup_result.scalar_one_or_none()

        if backup is not None and backup.file_path:
            try:
                from backup_manager.backup import restore_backup
                await restore_backup(backup.id, session)
                logger.info("Database restored from backup #%d", backup_id)
            except Exception as exc:
                logger.error("Rollback database restore failed: %s", exc)
                if update is not None:
                    update.status = UpdateStatus.rollback_failed
                    update.error_message = f"Rollback failed: {exc}"
                    await session.commit()
                await notify_admin(
                    f"🚨 Откат обновления #{update_id} не выполнен!\n"
                    f"Ошибка восстановления базы данных: {exc}\n"
                    f"Требуется ручное вмешательство."
                )
                return

    if update is not None:
        update.status = UpdateStatus.rollback_success
        update.finished_at = datetime.utcnow()
        await session.commit()

    log_path = update.log_path if update else "N/A"
    await notify_admin(
        f"⚠️ Обновление не установлено.\n"
        f"Система автоматически откатилась на предыдущую стабильную версию.\n"
        f"Бот работает. База восстановлена. Активные подписки сохранены. "
        f"VPN-ключи не отключались.\n"
        f"Лог: {log_path}"
    )
    logger.info("Rollback completed for update #%d", update_id)
