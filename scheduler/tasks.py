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


async def _job_retry_failed_activations() -> None:
    logger.info("Running: retry_failed_activations")
    from app.services.key_delivery import retry_failed_activations
    async with async_session_maker() as session:
        await retry_failed_activations(session)


async def _job_check_server_health() -> None:
    """Mark servers as unreachable if circuit breaker has been open too long."""
    logger.info("Running: check_server_health")
    from app.services.circuit_breaker import get_circuit_breaker, CircuitState
    from app.models.vpn_server import VpnServer
    from app.enums import VpnServerStatus
    from sqlalchemy import select
    from app.services.notification import notify_admin
    async with async_session_maker() as session:
        result = await session.execute(
            select(VpnServer).where(VpnServer.status == VpnServerStatus.active)
        )
        servers = result.scalars().all()
        for server in servers:
            cb = get_circuit_breaker(server.id)
            if cb.state == CircuitState.OPEN and (server.failure_count or 0) >= 10:
                server.status = VpnServerStatus.unreachable
                await notify_admin(
                    f"🔴 Сервер '{server.name}' (#{server.id}) переведён в UNREACHABLE "
                    f"после {server.failure_count} ошибок."
                )
        await session.commit()


async def _job_speed_limit_check() -> None:
    """Soft speed limit enforcement: disable keys exceeding speed quota."""
    logger.info("Running: speed_limit_check")
    from app.services.speed_monitor import check_speed_limits
    async with async_session_maker() as session:
        await check_speed_limits(session)


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(_job_expire_subscriptions, "cron", hour=2, minute=0)
    scheduler.add_job(_job_check_expiring_soon, "cron", hour=9, minute=0)
    scheduler.add_job(_job_sync_traffic, "cron", hour=3, minute=0)
    scheduler.add_job(_job_daily_backup, "cron", hour=4, minute=0)
    scheduler.add_job(_job_health_check, "interval", hours=1)
    scheduler.add_job(_job_collect_analytics, "cron", hour=23, minute=55)
    scheduler.add_job(_job_retry_failed_activations, "interval", minutes=15)
    scheduler.add_job(_job_check_server_health, "interval", hours=1, jitter=120)
    scheduler.add_job(_job_speed_limit_check, "interval", hours=1, jitter=60)
    return scheduler
