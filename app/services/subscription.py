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


async def _get_active_server(session: AsyncSession) -> VpnServer | None:
    result = await session.execute(
        select(VpnServer).where(VpnServer.status == VpnServerStatus.active).order_by(VpnServer.current_users)
    )
    return result.scalars().first()


async def activate_subscription(
    subscription: Subscription,
    tariff: Tariff,
    session: AsyncSession,
    send_key_callback: object | None = None,
) -> VpnKey | None:
    server = await _get_active_server(session)
    if server is None:
        subscription.status = SubscriptionStatus.activation_error
        await session.flush()
        await notify_admin(
            f"⚠️ Ошибка активации подписки #{subscription.id}: нет доступного VPN-сервера."
        )
        return None

    try:
        vpn_key = await vpn_key_service.create_vpn_key(
            user_id=subscription.user_id,
            subscription_id=subscription.id,
            server=server,
            traffic_limit_gb=tariff.traffic_limit_gb,
            session=session,
        )
    except Exception as exc:
        logger.error("VPN key creation failed for subscription %s: %s", subscription.id, exc)
        subscription.status = SubscriptionStatus.activation_error
        await session.flush()
        await notify_admin(
            f"⚠️ Ошибка создания VPN-ключа для подписки #{subscription.id}:\n{exc}"
        )
        return None

    subscription.status = SubscriptionStatus.active
    subscription.started_at = datetime.utcnow()
    subscription.expires_at = datetime.utcnow() + timedelta(days=tariff.duration_days)
    server.current_users += 1
    await session.flush()

    if send_key_callback is not None:
        try:
            await send_key_callback(subscription.user_id, vpn_key)
        except Exception as exc:
            logger.error("Failed to send key to user %s: %s", subscription.user_id, exc)

    return vpn_key


async def expire_subscriptions(session: AsyncSession, notify_user_callback: object | None = None) -> None:
    now = datetime.utcnow()
    result = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.vpn_keys))
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
                result2 = await session.execute(
                    select(VpnServer).where(VpnServer.id == key.server_id)
                )
                server = result2.scalar_one_or_none()
                if server is not None:
                    await vpn_key_service.disable_vpn_key(key, server, session)
                    server.current_users = max(0, server.current_users - 1)

        if notify_user_callback is not None:
            try:
                await notify_user_callback(sub.user_id)
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
                    await notify_user_callback(sub.user_id, days)
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
