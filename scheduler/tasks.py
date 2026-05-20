from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.database import async_session_maker

logger = logging.getLogger(__name__)


async def _job_expire_subscriptions() -> None:
    logger.info("Running: expire_subscriptions")
    from app.services.subscription import expire_subscriptions
    async with async_session_maker() as session:
        await expire_subscriptions(session)


async def _job_check_expiring_soon() -> None:
    logger.info("Running: check_expiring_soon")
    from app.services.subscription import check_expiring_soon
    async with async_session_maker() as session:
        await check_expiring_soon(session)


async def _job_sync_traffic() -> None:
    logger.info("Running: sync_traffic_stats")
    from app.services.vpn_key import sync_traffic_stats
    async with async_session_maker() as session:
        await sync_traffic_stats(session)


async def _job_check_traffic_limits() -> None:
    logger.info("Running: check_traffic_limits")
    from app.services.vpn_key import sync_traffic_stats
    async with async_session_maker() as session:
        await sync_traffic_stats(session)


async def _job_daily_backup() -> None:
    logger.info("Running: daily_backup")
    from app.enums import BackupType
    from backup_manager.backup import create_backup
    async with async_session_maker() as session:
        try:
            await create_backup(BackupType.daily, session)
        except Exception as exc:
            logger.error("Daily backup failed: %s", exc)
            from app.services.notification import notify_admin
            await notify_admin(f"❌ Ежедневный бэкап не создан: {exc}")


async def _job_health_check() -> None:
    logger.info("Running: health_check")
    from health_monitor.checker import run_health_check
    async with async_session_maker() as session:
        await run_health_check(session)


async def _job_collect_analytics() -> None:
    logger.info("Running: collect_daily_analytics")
    from app.services.analytics import collect_daily_analytics
    async with async_session_maker() as session:
        await collect_daily_analytics(session)


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(_job_expire_subscriptions, "cron", hour=2, minute=0)
    scheduler.add_job(_job_check_expiring_soon, "cron", hour=9, minute=0)
    scheduler.add_job(_job_sync_traffic, "cron", hour=3, minute=0)
    scheduler.add_job(_job_daily_backup, "cron", hour=4, minute=0)
    scheduler.add_job(_job_health_check, "interval", hours=1)
    scheduler.add_job(_job_collect_analytics, "cron", hour=23, minute=55)
    return scheduler
