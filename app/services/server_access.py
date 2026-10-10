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
from app.models.tariff import Tariff
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
        or server.provider == "private-line-primary"
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

    if server.max_users and server.current_users >= server.max_users:
        raise ServerSelectionError("На выбранном сервере закончились свободные места")

    tariff = await session.get(Tariff, subscription.tariff_id)
    if tariff is None:
        raise ServerSelectionError("У подписки не найден тариф")
    traffic_limit = getattr(tariff, "traffic_limit_gb", None)
    ip_limit = int(getattr(tariff, "max_devices", 0) or 0)

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

        # Validate published Marzban host BEFORE creating a remote user.
        # Otherwise users can be orphaned on a node with a private Docker address.
        config = await client._get_core_config()
        inbounds = [
            item for item in (config or {}).get("inbounds", [])
            if item.get("protocol") == "vless"
            and item.get("streamSettings", {}).get("security") == "reality"
        ]
        if len(inbounds) != 1 or not inbounds[0].get("tag"):
            raise ServerSelectionError("У VPN-ноды некорректный Reality inbound")
        host_resp = await client._client.get(
            f"{client._base}/api/hosts", headers=headers,
        )
        if host_resp.status_code != 200:
            raise ServerSelectionError("VPN-нода не публикует адрес для клиентов")
        entries = host_resp.json().get(inbounds[0]["tag"], [])
        if len(entries) != 1 or entries[0].get("is_disabled") is True:
            raise ServerSelectionError("VPN-нода не имеет активного публичного адреса")
        expected_host = entries[0].get("address", "")
        try:
            if not ipaddress.ip_address(expected_host).is_global:
                raise ValueError("not global")
        except ValueError as exc:
            raise ServerSelectionError("VPN-нода публикует внутренний адрес") from exc

        # A username is scoped to Marzban's own database (a separate database
        # on each server). Creating it on a second node does not affect Spain.
        username = f"u{user_id}s{subscription_id}"[:32]
        raw = await client.create_key(name=username, traffic_limit_gb=traffic_limit, ip_limit=ip_limit)
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
        if host != expected_host:
            raise ServerSelectionError("VLESS-ссылка не совпадает с публичным адресом ноды")
        canonical_client_link(raw.access_url, "Private Line VPN")

        key = VpnKey(
            user_id=user_id,
            subscription_id=subscription_id,
            server_id=server.id,
            outline_key_id=raw.id,
            access_url_encrypted=encrypt(raw.access_url),
            status=VpnKeyStatus.active,
            traffic_limit_gb=traffic_limit,
            label=server.name,
        )
        session.add(key)
        server.current_users += 1
        await session.flush()
        return key, True
    finally:
        await client.aclose()
