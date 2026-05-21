from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import VpnKeyStatus
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services import vpn_key as vpn_key_service

logger = logging.getLogger(__name__)

MAX_MIGRATIONS_PER_30_DAYS = 2
MIGRATION_WINDOW_DAYS = 30


async def count_recent_migrations(
    subscription_id: int,
    session: AsyncSession,
) -> int:
    cutoff = datetime.utcnow() - timedelta(days=MIGRATION_WINDOW_DAYS)
    result = await session.execute(
        select(func.count(VpnKey.id)).where(
            VpnKey.subscription_id == subscription_id,
            VpnKey.status.in_([VpnKeyStatus.disabled, VpnKeyStatus.reissued]),
            VpnKey.disabled_at >= cutoff,
        )
    )
    count = result.scalar() or 0
    return int(count)


async def migrate_key_to_server(
    vpn_key: VpnKey,
    new_server: VpnServer,
    session: AsyncSession,
) -> VpnKey | None:
    migration_count = await count_recent_migrations(vpn_key.subscription_id, session)
    if migration_count >= MAX_MIGRATIONS_PER_30_DAYS:
        logger.warning(
            "Migration limit reached for subscription %d (%d migrations in last %d days)",
            vpn_key.subscription_id,
            migration_count,
            MIGRATION_WINDOW_DAYS,
        )
        raise ValueError(
            f"Достигнут лимит смены сервера: максимум {MAX_MIGRATIONS_PER_30_DAYS} раза за {MIGRATION_WINDOW_DAYS} дней."
        )

    if new_server.id == vpn_key.server_id:
        raise ValueError("Новый сервер совпадает с текущим.")

    old_server = await session.get(VpnServer, vpn_key.server_id)
    if old_server is None:
        raise ValueError(f"Current server {vpn_key.server_id} not found.")

    old_label = vpn_key.label

    try:
        new_key = await vpn_key_service.create_vpn_key(
            user_id=vpn_key.user_id,
            subscription_id=vpn_key.subscription_id,
            server=new_server,
            traffic_limit_gb=vpn_key.traffic_limit_gb,
            label=old_label,
            session=session,
        )
    except Exception as exc:
        logger.error(
            "Failed to create key on new server %d for subscription %d: %s",
            new_server.id,
            vpn_key.subscription_id,
            exc,
        )
        return None

    try:
        await vpn_key_service.disable_vpn_key(vpn_key, old_server, session)
        old_server.current_users = max(0, old_server.current_users - 1)
    except Exception as exc:
        logger.error(
            "Failed to disable old key %d during migration: %s",
            vpn_key.id,
            exc,
        )

    new_server.current_users += 1
    new_key.last_reissued_at = datetime.utcnow()
    await session.flush()

    logger.info(
        "Migrated key for subscription %d from server %d to server %d (new key %d)",
        vpn_key.subscription_id,
        vpn_key.server_id,
        new_server.id,
        new_key.id,
    )
    return new_key
