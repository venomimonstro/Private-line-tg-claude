from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Union
from urllib.parse import urlparse, parse_qs as _parse_qs

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.encryption import decrypt, encrypt
from app.enums import VpnKeyStatus, VpnServerStatus
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.circuit_breaker import CircuitOpenError, get_circuit_breaker
from app.vpn.outline import OutlineClient
from app.vpn.marzban import MarzbanClient, MarzbanConfigError, MarzbanKey

logger = logging.getLogger(__name__)

_KEY_REISSUE_COOLDOWN_DAYS = 7


def _key_url_needs_repair(access_url: str) -> bool:
    """Return True only if the stored URL is broken and needs repair.

    A URL is considered healthy if:
    - It is non-empty
    - Its SNI parameter contains no Markdown brackets
    - It contains the required Reality params (pbk + sid)

    We deliberately skip healthy keys so the repair job never issues a
    Marzban PUT request (which disrupts active connections) for keys that
    are already working.
    """
    if not access_url:
        return True
    try:
        parsed = urlparse(access_url)
        if parsed.scheme != "vless":
            return False  # Outline or unknown — don't touch
        qs = _parse_qs(parsed.query, keep_blank_values=True)
        sni = qs.get("sni", [""])[0]
        if "[" in sni or "]" in sni:
            return True  # Corrupted Markdown SNI
        if not qs.get("pbk", [""])[0] or "sid" not in qs:
            return True  # Empty sid is valid when allowed by the node.
        if (not parsed.username or not parsed.hostname or not parsed.port
            or not sni or qs.get("security") != ["reality"]
            or qs.get("flow") != ["xtls-rprx-vision"]):
            return True
        return False
    except Exception:
        return True  # Can't parse → assume broken
_START_TARIFF_SLUG = "start"

AnyClient = Union[OutlineClient, MarzbanClient]


def _server_protocol(server: VpnServer) -> str:
    """Detect protocol from encrypted credentials. Returns 'marzban' or 'outline'."""
    try:
        data = json.loads(decrypt(server.api_secret_encrypted))
        return data.get("protocol", "outline")
    except Exception:
        return "outline"


def _make_client(server: VpnServer) -> AnyClient:
    if _server_protocol(server) == "marzban":
        creds = json.loads(decrypt(server.api_secret_encrypted))
        return MarzbanClient(
            api_url=server.api_url,
            username=creds["username"],
            password=creds["password"],
        )
    return OutlineClient(api_url=server.api_url)


def _marzban_username(user_id: int, subscription_id: int) -> str:
    """Build a valid Marzban username (3-32 chars, alphanumeric + underscore)."""
    return f"u{user_id}s{subscription_id}"[:32]


async def create_vpn_key(
    user_id: int,
    subscription_id: int,
    server: VpnServer,
    traffic_limit_gb: int | None,
    session: AsyncSession,
    label: str | None = None,
    ip_limit: int = 0,
) -> VpnKey:
    # Replacing a key on one node MUST NOT delete keys for other nodes.
    # A subscription can have one active key per server (temporary fallback).
    old_keys_result = await session.execute(
        select(VpnKey).where(
            VpnKey.subscription_id == subscription_id,
            VpnKey.server_id == server.id,
        )
    )
    # Transfer counters from the active key so they survive purge
    _today = date.today()
    _inherited_reissue_count: int = 0
    _inherited_reissue_date: date | None = None
    _inherited_traffic_gb: float = 0.0
    for old_key in old_keys_result.scalars().all():
        if old_key.status == VpnKeyStatus.active:
            # Capture reissue tracking before deletion
            if old_key.reissue_date == _today:
                _inherited_reissue_count = old_key.reissue_count_today or 0
                _inherited_reissue_date = _today
            # Preserve accumulated traffic so the subscription limit stays accurate
            _inherited_traffic_gb = old_key.traffic_used_gb or 0.0
            # Best-effort: remove from VPN server
            try:
                old_server_result = await session.get(VpnServer, old_key.server_id)
                if old_server_result is not None:
                    await disable_vpn_key(old_key, old_server_result, session)
            except Exception as exc:
                logger.warning("Could not disable old key %d: %s", old_key.id, exc)
                old_key.status = VpnKeyStatus.disabled
                old_key.disabled_at = datetime.utcnow()
        await session.delete(old_key)
    await session.flush()

    client = _make_client(server)
    cb = get_circuit_breaker(server.id)
    protocol = _server_protocol(server)

    try:
        async def _create():
            if protocol == "marzban":
                assert isinstance(client, MarzbanClient)
                name = _marzban_username(user_id, subscription_id)
                return await client.create_key(name=name, traffic_limit_gb=traffic_limit_gb, ip_limit=ip_limit)
            else:
                assert isinstance(client, OutlineClient)
                key_name = f"user_{user_id}_sub_{subscription_id}"
                if label:
                    key_name = f"{key_name}_{label}"
                outline_key = await client.create_key(name=key_name)
                if traffic_limit_gb is not None and traffic_limit_gb > 0:
                    await client.set_data_limit(outline_key.id, traffic_limit_gb * 1024 ** 3)
                return outline_key

        raw_key = await cb.call(_create())
    except Exception as exc:
        if not isinstance(exc, (CircuitOpenError, MarzbanConfigError)):
            server.failure_count += 1
            server.last_error = str(exc)[:500]
            server.last_error_at = datetime.utcnow()
        elif isinstance(exc, MarzbanConfigError):
            # Configuration errors count as server failures too
            server.failure_count += 1
            server.last_error = str(exc)[:500]
            server.last_error_at = datetime.utcnow()
        try:
            from app.services.outline_queue import enqueue_create_key
            await enqueue_create_key(
                subscription_id=subscription_id,
                server_id=server.id,
                payload={"user_id": user_id, "traffic_limit_gb": traffic_limit_gb, "label": label},
                session=session,
            )
        except Exception as enqueue_exc:
            logger.error("Failed to enqueue pending operation: %s", enqueue_exc)
        raise
    finally:
        await client.aclose()

    # Guard: never store an empty or non-VLESS URL — it would look active but be broken
    if not raw_key.access_url:
        raise ValueError(
            f"VPN client returned empty access_url for subscription {subscription_id} "
            f"on server {server.id}. Key NOT stored."
        )
    if protocol == "marzban" and not raw_key.access_url.startswith("vless://"):
        raise ValueError(
            f"Marzban returned unexpected access_url prefix for subscription "
            f"{subscription_id}: {raw_key.access_url[:60]}"
        )

    logger.info(
        "Storing key for sub %d user %d: %s...%s",
        subscription_id, user_id,
        raw_key.access_url[:40],
        raw_key.access_url[-20:] if len(raw_key.access_url) > 60 else "",
    )

    vpn_key = VpnKey(
        user_id=user_id,
        subscription_id=subscription_id,
        server_id=server.id,
        outline_key_id=raw_key.id,           # stores Marzban username or Outline key id
        access_url_encrypted=encrypt(raw_key.access_url),
        status=VpnKeyStatus.active,
        traffic_used_gb=_inherited_traffic_gb,
        traffic_limit_gb=traffic_limit_gb,
        label=label,
        reissue_count_today=_inherited_reissue_count,
        reissue_date=_inherited_reissue_date,
    )
    session.add(vpn_key)
    await session.flush()
    return vpn_key


async def disable_vpn_key(vpn_key: VpnKey, server: VpnServer, session: AsyncSession) -> None:
    client = _make_client(server)
    try:
        if isinstance(client, MarzbanClient):
            await client.delete_key(vpn_key.outline_key_id)
        else:
            await client.delete_key(vpn_key.outline_key_id)
    except Exception as exc:
        logger.error("Failed to delete key %s: %s", vpn_key.outline_key_id, exc)
        try:
            from app.models.pending_operation import PendingOperation, PendingOperationType
            op = PendingOperation(
                operation_type=PendingOperationType.disable_key.value,
                vpn_key_id=vpn_key.id,
                server_id=server.id,
                payload={},
                attempts=0,
                next_retry_at=datetime.utcnow(),
            )
            session.add(op)
        except Exception as enqueue_exc:
            logger.error("Failed to enqueue disable_key operation: %s", enqueue_exc)
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

    old_label = vpn_key.label
    await disable_vpn_key(vpn_key, server, session)
    vpn_key.status = VpnKeyStatus.reissued

    new_key = await create_vpn_key(
        user_id=vpn_key.user_id,
        subscription_id=vpn_key.subscription_id,
        server=server,
        traffic_limit_gb=traffic_limit_gb,
        label=old_label,
        session=session,
    )
    new_key.last_reissued_at = datetime.utcnow()
    return new_key


async def sync_traffic_stats(session: AsyncSession) -> None:
    result = await session.execute(
        select(VpnServer).where(VpnServer.status == VpnServerStatus.active)
    )
    servers = result.scalars().all()

    for server in servers:
        cb = get_circuit_breaker(server.id)
        if not cb.is_available():
            logger.warning("Skipping traffic sync for server %d (circuit open)", server.id)
            continue

        client = _make_client(server)
        try:
            stats: dict[str, int] = await cb.call(client.get_transfer_stats())
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

        keys_result = await session.execute(
            select(VpnKey).where(
                VpnKey.server_id == server.id,
                VpnKey.status == VpnKeyStatus.active,
            )
        )
        key_map: dict[str, VpnKey] = {k.outline_key_id: k for k in keys_result.scalars().all()}

        for key_id, bytes_used in stats.items():
            key = key_map.get(str(key_id))
            if key is not None:
                key.traffic_used_gb = bytes_used / (1024 ** 3)
                key.last_checked_at = datetime.utcnow()

    await session.commit()


def get_access_url(vpn_key: VpnKey) -> str:
    return decrypt(vpn_key.access_url_encrypted)

def get_client_access_url(vpn_key: VpnKey) -> str:
    """Return an import-ready link, without modifying the stored credentials."""
    from app.services.vpn_link import canonical_client_link

    return canonical_client_link(
        get_access_url(vpn_key),
        # One consistent profile name in every app. Show node/country in bot UI instead.
        label="Private Line VPN",
    )


async def repair_marzban_keys(
    session: AsyncSession,
    server_id: int | None = None,
) -> tuple[int, int, list[tuple[int, str]]]:
    """Reconcile active subscriptions with node users, preserving issued UUIDs.

    A syntactically valid link is not proof that its UUID exists on the node.
    Never reactivate expired/blocked subscriptions or exhausted traffic quotas.
    """
    from uuid import UUID
    from app.enums import SubscriptionStatus
    from app.models.subscription import Subscription
    from app.models.user import User

    servers = (await session.execute(select(VpnServer).where(
        VpnServer.status == VpnServerStatus.active,
    ))).scalars().all()
    fixed, errors = 0, 0
    notifications: list[tuple[int, str]] = []
    for server in servers:
        if server_id is not None and server.id != server_id:
            continue
        if _server_protocol(server) != "marzban":
            continue
        client = _make_client(server)
        try:
            users = await client.get_users()
            desired = set((await client.get_inbounds()).get("vless", []))
            if not desired:
                raise MarzbanConfigError("No VLESS inbounds")
            keys = (await session.execute(select(VpnKey).where(
                VpnKey.server_id == server.id,
                VpnKey.status == VpnKeyStatus.active,
            ))).scalars().all()
            for key in keys:
                try:
                    # Lock the subscription so concurrent expiry/reissue cannot
                    # race a network repair. Recheck eligibility after the lock.
                    subscription = (await session.execute(
                        select(Subscription).where(Subscription.id == key.subscription_id)
                        .with_for_update().execution_options(populate_existing=True)
                    )).scalar_one_or_none()
                    if (subscription is None
                        or subscription.user_id != key.user_id
                        or subscription.status not in (
                            SubscriptionStatus.active, SubscriptionStatus.expires_soon)
                        or subscription.expires_at is None
                        or subscription.expires_at <= datetime.utcnow()):
                        continue
                    await session.refresh(key)
                    if key.status != VpnKeyStatus.active:
                        continue
                    old_url = get_access_url(key)
                    parsed = urlparse(old_url)
                    if parsed.scheme != "vless":
                        raise MarzbanConfigError("Stored key is not VLESS")
                    original_uuid = str(UUID(parsed.username or ""))
                    current = users.get(key.outline_key_id)
                    used = max(key.traffic_used_gb or 0,
                               (current or {}).get("used_traffic", 0) / 1024**3)
                    limit = key.traffic_limit_gb or 0
                    if limit > 0 and used >= limit:
                        continue
                    # Preserve provider-side quota/expiry decisions as well.
                    if current and current.get("status") in ("limited", "expired", "disabled"):
                        logger.warning("Skipping inactive node user for key %d", key.id)
                        continue
                    proxy = (current or {}).get("proxies", {}).get("vless", {})
                    mismatch = (current is None
                        or proxy.get("id") != original_uuid
                        or proxy.get("flow") != "xtls-rprx-vision"
                        or not desired.issubset(set(current.get("inbounds", {}).get("vless", []))))
                    if not mismatch and not _key_url_needs_repair(old_url):
                        continue
                    # Recreated users start at zero node traffic: grant only
                    # their remaining quota, never the original full allowance.
                    remaining = max(0, limit - used) if limit else None
                    raw = await client.create_key(
                        name=key.outline_key_id, uuid=original_uuid,
                        traffic_limit_gb=remaining,
                    )
                    if str(UUID(urlparse(raw.access_url).username or "")) != original_uuid:
                        raise MarzbanConfigError("Repair returned a different VLESS UUID")
                    if old_url != raw.access_url:
                        key.access_url_encrypted = encrypt(raw.access_url)
                        user = await session.get(User, key.user_id)
                        if user and user.telegram_id:
                            notifications.append((user.telegram_id, raw.access_url))
                    fixed += 1
                except Exception as exc:
                    logger.error("Repair failed for key %d: %s", key.id, type(exc).__name__)
                    errors += 1
        except Exception as exc:
            logger.error("Repair failed for server %d: %s", server.id, type(exc).__name__)
            errors += 1
        finally:
            await client.aclose()
    await session.commit()
    return fixed, errors, notifications
