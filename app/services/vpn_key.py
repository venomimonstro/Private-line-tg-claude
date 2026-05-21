from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.encryption import decrypt, encrypt
from app.enums import VpnKeyStatus
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.circuit_breaker import get_circuit_breaker, CircuitOpenError
from app.vpn.outline import OutlineClient

logger = logging.getLogger(__name__)

_KEY_REISSUE_COOLDOWN_DAYS = 7
_START_TARIFF_SLUG = "start"


def _make_client(server: VpnServer) -> OutlineClient:
    return OutlineClient(api_url=server.api_url)


async def create_vpn_key(
    user_id: int,
    subscription_id: int,
    server: VpnServer,
    traffic_limit_gb: int | None,
    session: AsyncSession,
) -> VpnKey:
    client = _make_client(server)
    cb = get_circuit_breaker(server.id)
    try:
        async def _create():
            outline_key = await client.create_key(name=f"user_{user_id}_sub_{subscription_id}")
            if traffic_limit_gb is not None and traffic_limit_gb > 0:
                limit_bytes = traffic_limit_gb * 1024 * 1024 * 1024
                await client.set_data_limit(outline_key.id, limit_bytes)
            return outline_key

        outline_key = await cb.call(_create())
    except Exception as exc:
        if not isinstance(exc, CircuitOpenError):
            server.failure_count += 1
            server.last_error = str(exc)[:500]
            server.last_error_at = datetime.utcnow()
        raise
    finally:
        await client.aclose()

    vpn_key = VpnKey(
        user_id=user_id,
        subscription_id=subscription_id,
        server_id=server.id,
        outline_key_id=outline_key.id,
        access_url_encrypted=encrypt(outline_key.access_url),
        status=VpnKeyStatus.active,
        traffic_used_gb=0.0,
        traffic_limit_gb=traffic_limit_gb,
    )
    session.add(vpn_key)
    await session.flush()
    return vpn_key


async def disable_vpn_key(vpn_key: VpnKey, server: VpnServer, session: AsyncSession) -> None:
    client = _make_client(server)
    try:
        await client.delete_key(vpn_key.outline_key_id)
    except Exception as exc:
        logger.error("Failed to delete Outline key %s: %s", vpn_key.outline_key_id, exc)
    finally:
        await client.aclose()

    vpn_key.status = VpnKeyStatus.disabled
    vpn_key.disabled_at = datetime.utcnow()
    await session.flush()


async def reissue_vpn_key(
    vpn_key: VpnKey,
    server: VpnServer,
    tariff_slug: str,
    traffic_limit_gb: int | None,
    session: AsyncSession,
) -> VpnKey:
    if tariff_slug == _START_TARIFF_SLUG and vpn_key.created_at:
        cooldown_until = vpn_key.created_at + timedelta(days=_KEY_REISSUE_COOLDOWN_DAYS)
        if datetime.utcnow() < cooldown_until:
            raise ValueError(
                f"Перевыпуск ключа для тарифа «Старт» доступен раз в {_KEY_REISSUE_COOLDOWN_DAYS} дней."
            )

    await disable_vpn_key(vpn_key, server, session)
    vpn_key.status = VpnKeyStatus.reissued

    new_key = await create_vpn_key(
        user_id=vpn_key.user_id,
        subscription_id=vpn_key.subscription_id,
        server=server,
        traffic_limit_gb=traffic_limit_gb,
        session=session,
    )
    new_key.last_reissued_at = datetime.utcnow()
    return new_key


async def sync_traffic_stats(session: AsyncSession) -> None:
    result = await session.execute(
        select(VpnServer).where(VpnServer.status == "active")
    )
    servers = result.scalars().all()

    for server in servers:
        cb = get_circuit_breaker(server.id)
        if not cb.is_available():
            logger.warning("Skipping traffic sync for server %d (circuit open)", server.id)
            continue

        client = _make_client(server)
        try:
            stats = await cb.call(client.get_transfer_stats())
            server.failure_count = 0
            server.last_error = None
        except Exception as exc:
            logger.error("Failed to get traffic stats from server %s: %s", server.id, exc)
            if not isinstance(exc, CircuitOpenError):
                server.failure_count += 1
                server.last_error = str(exc)[:500]
                server.last_error_at = datetime.utcnow()
            continue
        finally:
            await client.aclose()

        for outline_key_id, bytes_used in stats.items():
            key_result = await session.execute(
                select(VpnKey).where(
                    VpnKey.server_id == server.id,
                    VpnKey.outline_key_id == outline_key_id,
                    VpnKey.status == VpnKeyStatus.active,
                )
            )
            key = key_result.scalar_one_or_none()
            if key is not None:
                key.traffic_used_gb = bytes_used / (1024 ** 3)
                key.last_checked_at = datetime.utcnow()

    await session.commit()


def get_access_url(vpn_key: VpnKey) -> str:
    return decrypt(vpn_key.access_url_encrypted)
