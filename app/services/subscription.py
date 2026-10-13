from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.enums import SubscriptionStatus, VpnKeyStatus, VpnServerStatus
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services import vpn_key as vpn_key_service
from app.services.notification import notify_admin

logger = logging.getLogger(__name__)

_EXPIRY_WARNING_DAYS = [3, 1]

_KEY_LABELS = ["Ключ 1", "Ключ 2", "Ключ 3", "Ключ 4", "Ключ 5"]


async def _get_available_servers(session: AsyncSession) -> list[VpnServer]:
    """Return active servers not in circuit-open state, ordered by load."""
    from app.services.circuit_breaker import get_circuit_breaker
    result = await session.execute(
        select(VpnServer)
        .where(
            VpnServer.status == VpnServerStatus.active,
            VpnServer.is_selectable.is_(True),
            VpnServer.provider != "private-line-primary",
        )
        .order_by(VpnServer.current_users)
    )
    servers = result.scalars().all()
    now = datetime.utcnow()
    available = [
        s for s in servers
        if vpn_key_service._server_protocol(s) == "direct_xray"
        and get_circuit_breaker(s.id).is_available()
        and (not s.max_users or s.current_users < s.max_users)
        and s.last_e2e_success_at is not None
        and s.last_e2e_success_at >= now - timedelta(minutes=15)
    ]
    return sorted(available, key=lambda s: s.current_users)


async def ensure_subscription_vpn_key(
    subscription: Subscription,
    tariff: Tariff,
    session: AsyncSession,
) -> VpnKey | None:
    """Guarantee at least one remotely coherent key without changing dates/status.

    Used for renewals where billing has succeeded but subscription duration
    must not be reset by a fresh activation.
    """
    rows = (await session.execute(
        select(VpnKey, VpnServer)
        .join(VpnServer, VpnServer.id == VpnKey.server_id)
        .where(
            VpnKey.subscription_id == subscription.id,
            VpnKey.user_id == subscription.user_id,
            VpnKey.status == VpnKeyStatus.active,
            VpnServer.status != VpnServerStatus.disabled,
            VpnServer.provider != "private-line-primary",
        )
        .order_by(VpnKey.id.asc())
    )).all()

    verified: list[VpnKey] = []
    verified_server_ids: set[int] = set()

    # Preserve every issued key, but count only keys on currently admitted,
    # recently-proven nodes as healthy replicas. Quarantined keys remain
    # deliverable for recovery but never suppress additive failover.
    proof_now = datetime.utcnow()
    for key, server in rows:
        if vpn_key_service._server_protocol(server) != "direct_xray":
            continue
        if (
            server.status != VpnServerStatus.active
            or not server.is_selectable
            or server.last_e2e_success_at is None
            or server.last_e2e_success_at < proof_now - timedelta(minutes=15)
        ):
            continue
        try:
            client = vpn_key_service._make_client(server)
            try:
                remote = await client.get_key(key.outline_key_id)
                if remote.access_url.startswith("vless://"):
                    verified.append(key)
                    verified_server_ids.add(server.id)
            finally:
                await client.aclose()
        except Exception as exc:
            logger.warning(
                "Existing direct Xray renewal key %d failed verification on server %d: %s",
                key.id, server.id, type(exc).__name__,
            )

    available = await _get_available_servers(session)
    candidate_server_ids = verified_server_ids | {server.id for server in available}
    desired_keys = min(
        max(1, int(tariff.max_keys or 1)),
        max(1, len(candidate_server_ids)),
    )
    # Fill missing redundant keys without rotating or removing any valid
    # credential. Each backup is created on a different healthy node.
    for server in available:
        if len(verified) >= desired_keys:
            break
        if server.id in verified_server_ids:
            continue
        try:
            key = await vpn_key_service.create_vpn_key(
                user_id=subscription.user_id,
                subscription_id=subscription.id,
                server=server,
                traffic_limit_gb=tariff.traffic_limit_gb,
                label=f"{server.flag_emoji or '🌍'} {server.country} • Авто-резерв",
                session=session,
                ip_limit=getattr(tariff, "max_devices", 0) or 0,
            )
            verified.append(key)
            verified_server_ids.add(server.id)
        except Exception as exc:
            logger.warning(
                "Renewal fallback server %d failed for subscription %d: %s",
                server.id, subscription.id, type(exc).__name__,
            )

    if verified and len(verified) < desired_keys:
        await notify_admin(
            f"⚠️ Renewal subscription #{subscription.id}: redundancy degraded "
            f"({len(verified)}/{desired_keys} VPN keys)."
        )
    return verified[0] if verified else None


async def activate_subscription(
    subscription: Subscription,
    tariff: Tariff,
    session: AsyncSession,
    send_key_callback: object | None = None,
) -> VpnKey | None:
    servers = await _get_available_servers(session)
    if not servers:
        subscription.status = SubscriptionStatus.activation_error
        subscription.activation_attempts = (subscription.activation_attempts or 0) + 1
        subscription.last_activation_error = "No available VPN servers"
        await session.flush()
        await notify_admin(
            f"⚠️ Ошибка активации подписки #{subscription.id}: нет доступного VPN-сервера."
        )
        return None

    created_keys: list[VpnKey] = []
    last_exc: Exception | None = None
    desired_keys = min(max(1, int(tariff.max_keys or 1)), len(servers))

    # Provision across distinct healthy nodes up to the tariff's key allowance.
    # This was historically stopped after the first success, which silently
    # disabled the project's existing multi-node redundancy model.
    for server in servers:
        if len(created_keys) >= desired_keys:
            break
        role = "Основной" if not created_keys else f"Резерв {len(created_keys)}"
        label = f"{server.flag_emoji or '🌍'} {server.country} • {role}"
        try:
            key = await vpn_key_service.create_vpn_key(
                user_id=subscription.user_id,
                subscription_id=subscription.id,
                server=server,
                traffic_limit_gb=tariff.traffic_limit_gb,
                label=label,
                session=session,
                ip_limit=getattr(tariff, "max_devices", 0) or 0,
            )
            created_keys.append(key)
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "Server %d failed for subscription %d while building %d-key redundancy: %s",
                server.id, subscription.id, desired_keys, exc,
            )

    if not created_keys:
        subscription.status = SubscriptionStatus.activation_error
        subscription.activation_attempts = (subscription.activation_attempts or 0) + 1
        subscription.last_activation_error = str(last_exc)[:500] if last_exc else "All servers failed"
        await session.flush()
        await notify_admin(
            f"⚠️ Subscription #{subscription.id}: all {len(servers)} server(s) failed. "
            f"Last error: {last_exc}"
        )
        try:
            from app.models.user import User
            from app.services.user_notifications import notify_activation_failed
            user = await session.get(User, subscription.user_id)
            if user and user.telegram_id:
                await notify_activation_failed(user.telegram_id)
        except Exception as notify_exc:
            logger.warning("Could not notify user of activation failure: %s", notify_exc)
        return None

    if len(created_keys) < desired_keys:
        await notify_admin(
            f"⚠️ Subscription #{subscription.id}: service activated, but redundancy "
            f"is degraded ({len(created_keys)}/{desired_keys} VPN keys)."
        )

    subscription.status = SubscriptionStatus.active
    subscription.last_activation_error = None
    subscription.activation_attempts = 0
    subscription.started_at = datetime.utcnow()
    subscription.expires_at = datetime.utcnow() + timedelta(days=tariff.duration_days)
    await session.flush()

    if send_key_callback is not None:
        try:
            from app.models.user import User
            user = await session.get(User, subscription.user_id)
            if user is None or not user.telegram_id:
                raise ValueError(f"No Telegram ID for user {subscription.user_id}")
            await send_key_callback(user.telegram_id, created_keys)
        except Exception as exc:
            logger.error("Failed to deliver VPN key for subscription %s: %s", subscription.id, exc)

    return created_keys[0]


async def expire_subscriptions(session: AsyncSession, notify_user_callback: object | None = None) -> None:
    now = datetime.utcnow()
    result = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.vpn_keys).selectinload(VpnKey.server))
        .where(
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
            Subscription.expires_at <= now,
        )
    )
    subscriptions = result.scalars().all()

    for sub in subscriptions:
        sub.status = SubscriptionStatus.expired
        for key in sub.vpn_keys:
            if key.status == VpnKeyStatus.active:
                server = key.server
                if server is not None:
                    disabled = await vpn_key_service.disable_vpn_key(key, server, session)
                    if not disabled:
                        logger.error(
                            "Expiry could not confirm remote disable for key %d on server %d; "
                            "capacity counter preserved",
                            key.id, server.id,
                        )

        if notify_user_callback is not None:
            try:
                user = await session.get(User, sub.user_id)
                if user and user.telegram_id:
                    await notify_user_callback(user.telegram_id)
            except Exception as exc:
                logger.error("Failed to notify user %s about expiry: %s", sub.user_id, exc)

    await session.commit()
    logger.info("Expired %d subscriptions", len(subscriptions))


async def check_expiring_soon(session: AsyncSession, notify_user_callback: object | None = None) -> None:
    now = datetime.utcnow()
    for days in _EXPIRY_WARNING_DAYS:
        window_start = now + timedelta(days=days) - timedelta(hours=12)
        window_end = now + timedelta(days=days) + timedelta(hours=12)
        result = await session.execute(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.active,
                Subscription.expires_at >= window_start,
                Subscription.expires_at <= window_end,
            )
        )
        subscriptions = result.scalars().all()
        for sub in subscriptions:
            sub.status = SubscriptionStatus.expires_soon
            if notify_user_callback is not None:
                try:
                    user = await session.get(User, sub.user_id)
                    if user and user.telegram_id:
                        await notify_user_callback(user.telegram_id, days)
                except Exception as exc:
                    logger.error("Failed to notify user %s about expiry soon: %s", sub.user_id, exc)

    await session.commit()


async def extend_subscription(
    subscription: Subscription,
    days: int,
    session: AsyncSession,
) -> None:
    base = subscription.expires_at if subscription.expires_at and subscription.expires_at > datetime.utcnow() else datetime.utcnow()
    subscription.expires_at = base + timedelta(days=days)
    subscription.status = SubscriptionStatus.active
    await session.flush()
