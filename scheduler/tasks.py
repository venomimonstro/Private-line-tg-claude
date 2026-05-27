from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.database import async_session_maker
from app.services.key_delivery import RETRY_INTERVAL_MINUTES

logger = logging.getLogger(__name__)


async def _job_expire_subscriptions() -> None:
    logger.info("Running: expire_subscriptions")
    from app.services.subscription import expire_subscriptions
    from app.services.user_notifications import notify_expiry
    async with async_session_maker() as session:
        await expire_subscriptions(session, notify_user_callback=notify_expiry)


async def _job_check_expiring_soon() -> None:
    logger.info("Running: check_expiring_soon")
    from app.services.subscription import check_expiring_soon
    from app.services.user_notifications import notify_expiring_soon
    async with async_session_maker() as session:
        await check_expiring_soon(session, notify_user_callback=notify_expiring_soon)


async def _job_sync_traffic() -> None:
    logger.info("Running: sync_traffic_stats")
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
    from app.services.user_notifications import send_key_to_user
    async with async_session_maker() as session:
        await retry_failed_activations(session, send_key_callback=send_key_to_user)


async def _job_auto_renew_subscriptions() -> None:
    """Auto-renew subscriptions with auto_renew=True that expire in < 24h."""
    logger.info("Running: auto_renew_subscriptions")
    from app.services.subscription_renewal import auto_renew_subscriptions
    async with async_session_maker() as session:
        await auto_renew_subscriptions(session)


async def _job_check_server_health() -> None:
    logger.info("Running: check_server_health")
    from app.services.circuit_breaker import get_circuit_breaker, CircuitState
    from app.models.vpn_server import VpnServer
    from app.enums import VpnServerStatus
    from sqlalchemy import select
    from app.services.notification import notify_admin

    notifications: list[str] = []
    async with async_session_maker() as session:
        result = await session.execute(
            select(VpnServer).where(VpnServer.status == VpnServerStatus.active)
        )
        servers = result.scalars().all()
        for server in servers:
            cb = get_circuit_breaker(server.id)
            if cb.state == CircuitState.OPEN and (server.failure_count or 0) >= 10:
                server.status = VpnServerStatus.unreachable
                notifications.append(
                    f"🔴 Сервер '{server.name}' (#{server.id}) переведён в UNREACHABLE "
                    f"после {server.failure_count} ошибок."
                )
        await session.commit()

    if notifications:
        await asyncio.gather(*[notify_admin(msg) for msg in notifications])


async def _job_speed_limit_check() -> None:
    logger.info("Running: speed_limit_check")
    from app.services.speed_monitor import check_speed_limits
    async with async_session_maker() as session:
        await check_speed_limits(session)


async def _job_update_metrics() -> None:
    logger.info("Running: update_metrics")
    from app.metrics import active_subscriptions
    from app.enums import SubscriptionStatus
    from app.models.subscription import Subscription
    from sqlalchemy import func, select
    async with async_session_maker() as session:
        result = await session.execute(
            select(func.count(Subscription.id)).where(
                Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon])
            )
        )
        count = result.scalar_one() or 0
        active_subscriptions.set(count)


async def _job_check_traffic_limits() -> None:
    logger.info("Running: check_traffic_limits")
    from app.services.traffic_notifications import check_traffic_limits
    async with async_session_maker() as session:
        await check_traffic_limits(session)


async def _job_verify_pending_payments() -> None:
    logger.info("Running: verify_pending_payments")
    from app.services.payment_verification import verify_pending_payments
    async with async_session_maker() as session:
        await verify_pending_payments(session)


async def _job_process_pending_operations() -> None:
    logger.info("Running: process_pending_operations")
    try:
        from app.services.outline_queue import process_pending_operations
        async with async_session_maker() as session:
            await process_pending_operations(session)
    except ImportError:
        logger.debug("outline_queue module not available, skipping")


async def _job_repair_marzban_inbounds() -> None:
    """Fix existing Marzban users whose inbound tag was hardcoded to 'VLESS-TCP'.

    Runs immediately on startup (next_run_time=now) and then every 24 hours.
    This repairs keys created before the dynamic-inbound fix was deployed.
    """
    logger.info("Running: repair_marzban_inbounds")
    from app.enums import VpnServerStatus
    from app.models.vpn_server import VpnServer
    from app.services.notification import notify_admin
    from app.services.vpn_key import _server_protocol, _make_client

    async with async_session_maker() as session:
        from sqlalchemy import select as _select
        result = await session.execute(
            _select(VpnServer).where(VpnServer.status == VpnServerStatus.active)
        )
        servers = result.scalars().all()

    for server in servers:
        try:
            if _server_protocol(server) != "marzban":
                continue
            client = _make_client(server)
            try:
                repaired, errors = await client.repair_all_users_inbounds()
                if repaired > 0 or errors > 0:
                    await notify_admin(
                        f"🔧 Marzban inbound repair (server #{server.id} '{server.name}'):\n"
                        f"  Исправлено: {repaired} | Ошибок: {errors}"
                    )
            finally:
                await client.aclose()
        except Exception as exc:
            logger.error(
                "repair_marzban_inbounds failed for server %d '%s': %s",
                server.id, server.name, exc,
            )


def create_scheduler() -> AsyncIOScheduler:
    from datetime import datetime, timezone as _tz

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(_job_expire_subscriptions, "cron", hour=2, minute=0)
    scheduler.add_job(_job_check_expiring_soon, "cron", hour=9, minute=0)
    scheduler.add_job(_job_sync_traffic, "interval", hours=2)
    scheduler.add_job(_job_daily_backup, "cron", hour=4, minute=0)
    scheduler.add_job(_job_health_check, "interval", hours=1)
    scheduler.add_job(_job_collect_analytics, "cron", hour=23, minute=55)
    scheduler.add_job(_job_retry_failed_activations, "interval", minutes=RETRY_INTERVAL_MINUTES)
    scheduler.add_job(_job_auto_renew_subscriptions, "cron", hour=1, minute=30)
    scheduler.add_job(_job_check_server_health, "interval", hours=1, jitter=120)
    scheduler.add_job(_job_speed_limit_check, "interval", hours=1, jitter=60)
    scheduler.add_job(_job_update_metrics, "interval", minutes=5)
    scheduler.add_job(_job_check_traffic_limits, "interval", hours=4)
    scheduler.add_job(_job_verify_pending_payments, "interval", minutes=15)
    scheduler.add_job(_job_process_pending_operations, "interval", minutes=5)
    # Repair Marzban users with wrong inbounds immediately on startup, then daily.
    # This fixes keys created while "VLESS-TCP" was hardcoded.
    scheduler.add_job(
        _job_repair_marzban_inbounds,
        "interval",
        hours=24,
        next_run_time=datetime.now(_tz.utc),
    )
    return scheduler
