"""Add a VPN server to an active subscription without removing existing keys.

A paid subscription can contain one key per configured/selectable server.
The operation is fail-closed: user ownership, subscription validity, capacity,
core status, public VLESS endpoint and settings must pass verification first.
"""
from __future__ import annotations

import ipaddress
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.encryption import encrypt
from app.enums import SubscriptionStatus, VpnKeyStatus, VpnServerStatus
from app.models.subscription import Subscription
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _make_client, _server_protocol, get_client_access_url
from app.services.vpn_link import canonical_client_link


class ServerSelectionError(ValueError):
    pass


async def add_subscription_server_key(
    session: AsyncSession, *, user_id: int, subscription_id: int, server_id: int
) -> tuple[VpnKey, bool]:
    """Return (VPN key, was_created). Caller must commit transaction."""
    subscription = await session.scalar(
        select(Subscription).where(Subscription.id == subscription_id).with_for_update()
    )
    if subscription is None or subscription.user_id != user_id:
        raise ServerSelectionError("Подписка не принадлежит пользователю")
    if (
        subscription.status not in (SubscriptionStatus.active, SubscriptionStatus.expires_soon)
        or not subscription.expires_at
        or subscription.expires_at <= datetime.utcnow()
    ):
        raise ServerSelectionError("Подписка неактивна или истекла")

    server = await session.get(VpnServer, server_id)
    if (
        server is None or server.status != VpnServerStatus.active
        or not server.is_selectable
        or _server_protocol(server) != "marzban"
    ):
        raise ServerSelectionError("Этот VPN-сервер пока недоступен")

    keys = (await session.execute(select(VpnKey).where(
        VpnKey.user_id == user_id,
        VpnKey.subscription_id == subscription_id,
        VpnKey.server_id == server_id,
        VpnKey.status == VpnKeyStatus.active,
    ))).scalars().all()
    if len(keys) > 1:
        raise ServerSelectionError("Несколько ключей на одной ноде; требуется проверка")
    if keys:
        return keys[0], False

    count = await session.scalar(select(VpnKey.id).where(
        VpnKey.server_id == server_id,
        VpnKey.status == VpnKeyStatus.active,
    ).limit(1))
    if server.max_users and server.current_users >= server.max_users:
        raise ServerSelectionError("На выбранном сервере закончились свободные места")

    client = _make_client(server)
    try:
        if not await client.check_connection():
            raise ServerSelectionError("VPN-нода не отвечает, ключ не создавался")
        headers = await client._headers()
        response = await client._client.get(
            f"{client._base}/api/core", headers=headers,
        )
        if response.status_code != 200 or response.json().get("started") is not True:
            raise ServerSelectionError("Xray на выбранной ноде ещё не запущен")

        # A username is scoped to Marzban's own database (a separate database
        # on each server). Creating it on a second node does not affect Spain.
        username = f"u{user_id}s{subscription_id}"[:32]
        raw = await client.create_key(name=username, traffic_limit_gb=None)
        if not raw.access_url or not raw.access_url.startswith("vless://"):
            raise ServerSelectionError("Нода вернула некорректный VLESS-ключ")

        # Never hand out a Docker-internal address in a share link.
        # Marzban hosts must have the node's externally routable address.
        try:
            host = urlsplit(raw.access_url).hostname or ""
            if not ipaddress.ip_address(host).is_global:
                raise ValueError("internal or private IP")
        except ValueError:
            raise ServerSelectionError(
                "Нода не настроена для клиентов: адрес VPN не публичный"
            )
        canonical_client_link(raw.access_url, "Private Line VPN")

        key = VpnKey(
            user_id=user_id,
            subscription_id=subscription_id,
            server_id=server.id,
            outline_key_id=raw.id,
            access_url_encrypted=encrypt(raw.access_url),
            status=VpnKeyStatus.active,
            traffic_limit_gb=None,
            label=server.name,
        )
        session.add(key)
        server.current_users += 1
        await session.flush()
        return key, True
    finally:
        await client.aclose()
