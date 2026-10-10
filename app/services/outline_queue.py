from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pending_operation import PendingOperation, PendingOperationType

logger = logging.getLogger(__name__)

_BATCH_SIZE = 10


async def enqueue_create_key(
    subscription_id: int,
    server_id: int,
    payload: dict[str, Any],
    session: AsyncSession,
) -> PendingOperation:
    """Enqueue at most one live create operation per subscription/node.

    Repeated API failures must not build a retry storm that races the same
    Marzban username and later replays stale provisioning attempts.
    """
    existing = (await session.execute(
        select(PendingOperation).where(
            PendingOperation.operation_type == PendingOperationType.create_key.value,
            PendingOperation.subscription_id == subscription_id,
            PendingOperation.server_id == server_id,
            PendingOperation.completed_at.is_(None),
        ).order_by(PendingOperation.id.desc())
    )).scalars().first()
    if existing is not None:
        # Keep the latest desired non-secret metadata and bring an exhausted
        # transient operation back into a bounded retry window.
        existing.payload = payload
        if existing.attempts >= existing.max_attempts:
            existing.attempts = 0
            existing.last_error = None
        existing.next_retry_at = min(
            existing.next_retry_at or datetime.utcnow(),
            datetime.utcnow() + timedelta(minutes=2),
        )
        await session.flush()
        logger.info(
            "Reusing pending create operation %d for subscription %d on server %d",
            existing.id, subscription_id, server_id,
        )
        return existing

    op = PendingOperation(
        operation_type=PendingOperationType.create_key.value,
        subscription_id=subscription_id,
        server_id=server_id,
        payload=payload,
        attempts=0,
        next_retry_at=datetime.utcnow(),
    )
    session.add(op)
    await session.flush()
    logger.info(
        "Enqueued create_key operation %d for subscription %d on server %d",
        op.id, subscription_id, server_id,
    )
    return op


async def process_pending_operations(session: AsyncSession) -> int:
    """Process up to _BATCH_SIZE pending operations. Returns count processed."""
    now = datetime.utcnow()
    result = await session.execute(
        select(PendingOperation)
        .where(
            PendingOperation.completed_at.is_(None),
            PendingOperation.next_retry_at <= now,
            PendingOperation.attempts < PendingOperation.max_attempts,
        )
        .limit(_BATCH_SIZE)
    )
    ops = result.scalars().all()

    processed = 0
    for op in ops:
        try:
            await _execute_operation(op, session)
            op.completed_at = datetime.utcnow()
            processed += 1
            logger.info("Pending operation %d completed successfully", op.id)
        except ValueError as exc:
            # Permanent logic errors (missing fields, unknown type) — don't retry
            op.attempts = op.max_attempts  # exhaust retries immediately
            op.last_error = str(exc)[:500]
            op.completed_at = datetime.utcnow()  # mark done so it stops appearing
            logger.error(
                "Pending operation %d permanently failed (non-retryable): %s",
                op.id, exc,
            )
        except Exception as exc:
            op.attempts += 1
            op.last_error = str(exc)[:500]
            backoff_minutes = 2 ** op.attempts
            op.next_retry_at = datetime.utcnow() + timedelta(minutes=backoff_minutes)
            logger.warning(
                "Pending operation %d failed (attempt %d/%d): %s. Next retry in %d min",
                op.id,
                op.attempts,
                op.max_attempts,
                exc,
                backoff_minutes,
            )

    await session.commit()
    return processed


async def _execute_operation(op: PendingOperation, session: AsyncSession) -> None:
    """Execute a single pending operation."""
    from app.models.vpn_server import VpnServer
    from app.models.vpn_key import VpnKey
    from app.services import vpn_key as vpn_key_service

    op_type = op.operation_type

    if op_type == PendingOperationType.create_key.value:
        if op.server_id is None or op.subscription_id is None:
            raise ValueError("create_key operation missing server_id or subscription_id")

        server = await session.get(VpnServer, op.server_id)
        if server is None:
            raise ValueError(f"Server {op.server_id} not found")
        from app.enums import VpnServerStatus
        if server.status != VpnServerStatus.active or not server.is_selectable:
            raise ValueError("Queued VPN server is disabled or not selectable")
        if server.max_users and server.current_users >= server.max_users:
            raise ValueError("Queued VPN server is at capacity")

        payload = op.payload or {}
        user_id = payload.get("user_id")
        traffic_limit_gb = payload.get("traffic_limit_gb")
        label = payload.get("label")
        ip_limit = int(payload.get("ip_limit") or 0)

        if user_id is None:
            raise ValueError("create_key payload missing user_id")

        # A delayed create operation must not rotate/delete an already issued
        # active key. Previously create_vpn_key() purged it before its API call,
        # so stale retries could strand a paid user with an orphaned VLESS link.
        from app.enums import SubscriptionStatus, VpnKeyStatus
        from app.models.subscription import Subscription

        sub = await session.get(Subscription, op.subscription_id)
        if sub is None or sub.status not in (
            SubscriptionStatus.activation_error,
            SubscriptionStatus.active,
            SubscriptionStatus.expires_soon,
        ):
            raise ValueError("Queued creation requires an eligible subscription")
        if sub.status == SubscriptionStatus.activation_error:
            # Subscription-level recovery owns this state because it must set
            # dates/status and delivery atomically with successful provisioning.
            # A low-level queued create here would leave an active remote UUID
            # attached to an activation_error subscription.
            op.last_error = "delegated to subscription activation recovery"
            logger.info(
                "Pending create operation %d delegated to subscription recovery",
                op.id,
            )
            return
        if sub.user_id != int(user_id):
            raise ValueError("Queued creation user/subscription mismatch")
        if (
            sub.status in (SubscriptionStatus.active, SubscriptionStatus.expires_soon)
            and sub.expires_at is not None
            and sub.expires_at <= datetime.utcnow()
        ):
            raise ValueError("Subscription expired; refusing queued key creation")
        # Idempotency is per-node: a subscription may legitimately have
        # active keys on several selectable VPN nodes.
        existing = (await session.execute(
            select(VpnKey).where(
                VpnKey.subscription_id == op.subscription_id,
                VpnKey.server_id == op.server_id,
                VpnKey.status == VpnKeyStatus.active,
            )
        )).scalars().all()
        if existing:
            if len(existing) != 1:
                raise ValueError("Multiple active keys on one node require reconciliation")
            op.vpn_key_id = existing[0].id
            logger.info(
                "Skipping stale queued key creation for subscription %s on server %s: "
                "active key %s is already issued",
                op.subscription_id, op.server_id, existing[0].id,
            )
            return

        vpn_key = await vpn_key_service.create_vpn_key(
            user_id=int(user_id),
            subscription_id=op.subscription_id,
            server=server,
            traffic_limit_gb=traffic_limit_gb,
            label=label,
            session=session,
            ip_limit=ip_limit,
        )
        op.vpn_key_id = vpn_key.id
        logger.info("Created VPN key %d via pending operation %d", vpn_key.id, op.id)

        # Notify the user — they may have been waiting since payment without
        # receiving their key (pending operations run silently in the background)
        try:
            from app.models.user import User
            from app.services.user_notifications import send_key_to_user
            db_user = await session.get(User, int(user_id))
            if db_user is not None:
                await send_key_to_user(db_user.telegram_id, vpn_key)
                logger.info(
                    "Sent pending-op key to user %d (tg=%d)",
                    db_user.id, db_user.telegram_id,
                )
        except Exception as notify_exc:
            # Non-fatal: key was created, notification failed
            logger.warning(
                "Could not notify user %s after pending-op key creation: %s",
                user_id, notify_exc,
            )

    elif op_type == PendingOperationType.disable_key.value:
        if op.vpn_key_id is None:
            raise ValueError("disable_key operation missing vpn_key_id")

        vpn_key = await session.get(VpnKey, op.vpn_key_id)
        if vpn_key is None:
            raise ValueError(f"VpnKey {op.vpn_key_id} not found")

        server = await session.get(VpnServer, vpn_key.server_id)
        if server is None:
            raise ValueError(f"Server {vpn_key.server_id} not found")

        await vpn_key_service.disable_vpn_key(
            vpn_key, server, session,
            enqueue_on_failure=False,
            raise_on_failure=True,
        )
        logger.info("Disabled VPN key %d via pending operation %d", vpn_key.id, op.id)

    elif op_type == PendingOperationType.delete_key.value:
        if op.vpn_key_id is None:
            raise ValueError("delete_key operation missing vpn_key_id")

        vpn_key = await session.get(VpnKey, op.vpn_key_id)
        if vpn_key is None:
            raise ValueError(f"VpnKey {op.vpn_key_id} not found")

        server = await session.get(VpnServer, vpn_key.server_id)
        if server is None:
            raise ValueError(f"Server {vpn_key.server_id} not found")

        await vpn_key_service.disable_vpn_key(
            vpn_key, server, session,
            enqueue_on_failure=False,
            raise_on_failure=True,
        )
        logger.info("Disabled/deleted VPN key %d via pending operation %d", vpn_key.id, op.id)

    else:
        raise ValueError(f"Unknown operation type: {op_type}")
