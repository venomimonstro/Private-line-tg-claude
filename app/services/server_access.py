"""Provision an additional verified VPN node for an active subscription.

All remote creation goes through app.services.vpn_key.create_vpn_key so manual
server selection and automatic activation have identical safety checks.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import SubscriptionStatus, VpnKeyStatus, VpnServerStatus
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _server_protocol, create_vpn_key


class ServerSelectionError(ValueError):
    pass


async def add_subscription_server_key(
    session: AsyncSession, *, user_id: int, subscription_id: int, server_id: int
) -> tuple[VpnKey, bool]:
    """Return (key, was_created), preserving keys already active on other nodes."""
    subscription = await session.scalar(
        select(Subscription)
        .where(Subscription.id == subscription_id)
        .with_for_update()
    )
    if subscription is None or subscription.user_id != user_id:
        raise ServerSelectionError("Подписка не принадлежит пользователю")
    if (
        subscription.status not in (
            SubscriptionStatus.active,
            SubscriptionStatus.expires_soon,
        )
        or not subscription.expires_at
        or subscription.expires_at <= datetime.utcnow()
    ):
        raise ServerSelectionError("Подписка неактивна или истекла")

    server = await session.get(VpnServer, server_id)
    if (
        server is None
        or server.status != VpnServerStatus.active
        or not server.is_selectable
        or server.provider == "private-line-primary"
        or _server_protocol(server) != "marzban"
    ):
        raise ServerSelectionError("Этот VPN-сервер пока недоступен")
    if server.max_users and server.current_users >= server.max_users:
        raise ServerSelectionError("На выбранном сервере закончились свободные места")

    existing = (await session.execute(
        select(VpnKey).where(
            VpnKey.user_id == user_id,
            VpnKey.subscription_id == subscription_id,
            VpnKey.server_id == server_id,
            VpnKey.status == VpnKeyStatus.active,
        )
    )).scalars().all()
    if len(existing) > 1:
        raise ServerSelectionError(
            "На одной ноде найдено несколько активных ключей; требуется проверка"
        )
    if existing:
        return existing[0], False

    tariff = await session.get(Tariff, subscription.tariff_id)
    if tariff is None:
        raise ServerSelectionError("У подписки не найден тариф")

    try:
        key = await create_vpn_key(
            user_id=user_id,
            subscription_id=subscription_id,
            server=server,
            traffic_limit_gb=tariff.traffic_limit_gb,
            ip_limit=int(getattr(tariff, "max_devices", 0) or 0),
            label=server.name,
            session=session,
        )
    except Exception as exc:
        # Do not expose provider internals or credentials to Telegram users.
        raise ServerSelectionError(
            "VPN-нода не прошла проверку готовности. Текущие ключи сохранены."
        ) from exc

    # create_vpn_key owns the capacity counter transactionally.
    return key, True
