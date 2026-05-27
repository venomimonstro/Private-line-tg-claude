from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Union

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
    # Purge ALL old keys for this subscription (any server, any status).
    # One subscription = one key always. Old keys take up space and confuse users.
    old_keys_result = await session.execute(
        select(VpnKey).where(VpnKey.subscription_id == subscription_id)
    )
    # Transfer bot-reissue daily counter from the active key so it survives purge
    _today = date.today()
    _inherited_reissue_count: int = 0
    _inherited_reissue_date: date | None = None
    for old_key in old_keys_result.scalars().all():
        if old_key.status == VpnKeyStatus.active:
            # Capture reissue tracking before deletion
            if old_key.reissue_date == _today:
                _inherited_reissue_count = old_key.reissue_count_today or 0
                _inherited_reissue_date = _today
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
        traffic_used_gb=0.0,
        traffic_limit_gb=traffic_limit_gb,
        label=label,
        # Carry bot-reissue counter forward so the daily limit is preserved
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


async def repair_marzban_keys(
    session: AsyncSession,
) -> tuple[int, int, list[tuple[int, str]]]:
    """Re-issue correct access URLs for every active Marzban VPN key in the DB.

    For each active key on a Marzban server this function calls ``create_key()``
    with the existing Marzban username.  Because the user already exists, Marzban
    returns HTTP 409 → the code path in ``create_key()`` then:
      1. PUTs the user with the correct inbounds from ``/api/inbounds``
      2. GETs the updated user and extracts fresh links
      3. Injects Reality params from ``/api/core/config``
      4. Returns the corrected access URL

    The new URL is stored back into ``vpn_keys.access_url_encrypted``.

    Returns ``(fixed, errors, changed_notifications)`` where ``changed_notifications``
    is a list of ``(telegram_id, new_url)`` for keys whose URL was actually updated —
    callers should send these to the respective users.
    """
    from app.models.user import User

    # Load all active keys on Marzban servers
    result = await session.execute(
        select(VpnServer).where(VpnServer.status == VpnServerStatus.active)
    )
    servers = result.scalars().all()
    marzban_server_ids: set[int] = {s.id for s in servers if _server_protocol(s) == "marzban"}

    if not marzban_server_ids:
        logger.info("repair_marzban_keys: no active Marzban servers found — nothing to do")
        return 0, 0, []

    keys_result = await session.execute(
        select(VpnKey).where(
            VpnKey.server_id.in_(marzban_server_ids),
            VpnKey.status == VpnKeyStatus.active,
        )
    )
    keys: list[VpnKey] = list(keys_result.scalars().all())

    if not keys:
        logger.info("repair_marzban_keys: no active Marzban keys found — nothing to do")
        return 0, 0, []

    logger.info("repair_marzban_keys: repairing %d active Marzban keys", len(keys))

    # Build one client per server (reuse across keys on the same server)
    server_map: dict[int, VpnServer] = {s.id: s for s in servers if s.id in marzban_server_ids}
    clients: dict[int, MarzbanClient] = {}
    for sid, srv in server_map.items():
        try:
            clients[sid] = _make_client(srv)  # type: ignore[assignment]
        except Exception as exc:
            logger.error("repair_marzban_keys: cannot build client for server %d: %s", sid, exc)

    fixed = 0
    errors = 0
    # Collect (telegram_id, new_url) for keys whose stored URL actually changed.
    # We fetch telegram_id BEFORE commit while the session is still active.
    changed_notifications: list[tuple[int, str]] = []

    try:
        for key in keys:
            client = clients.get(key.server_id)
            if client is None:
                errors += 1
                continue
            try:
                raw = await client.create_key(name=key.outline_key_id)
                if raw.access_url:
                    old_url = decrypt(key.access_url_encrypted)
                    new_url = raw.access_url
                    if old_url != new_url:
                        key.access_url_encrypted = encrypt(new_url)
                        logger.info(
                            "repair_marzban_keys: updated URL for key %d (user=%s)",
                            key.id, key.outline_key_id,
                        )
                        # Collect for post-commit user notification
                        try:
                            user = await session.get(User, key.user_id)
                            if user is not None:
                                changed_notifications.append((user.telegram_id, new_url))
                        except Exception as exc:
                            logger.warning(
                                "repair_marzban_keys: could not load user for key %d: %s",
                                key.id, exc,
                            )
                    else:
                        logger.debug(
                            "repair_marzban_keys: key %d (user=%s) URL unchanged — already correct",
                            key.id, key.outline_key_id,
                        )
                    fixed += 1
                else:
                    logger.warning(
                        "repair_marzban_keys: key %d (user=%s) got empty access_url — skipping",
                        key.id, key.outline_key_id,
                    )
                    errors += 1
            except Exception as exc:
                logger.error(
                    "repair_marzban_keys: failed for key %d (user=%s): %s",
                    key.id, key.outline_key_id, exc,
                )
                errors += 1
    finally:
        for client in clients.values():
            try:
                await client.aclose()
            except Exception:
                pass

    await session.commit()
    logger.info(
        "repair_marzban_keys complete: %d fixed, %d errors, %d URLs changed (of %d total)",
        fixed, errors, len(changed_notifications), len(keys),
    )
    return fixed, errors, changed_notifications
