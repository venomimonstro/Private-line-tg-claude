from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Union
from urllib.parse import urlparse, parse_qs as _parse_qs

from sqlalchemy import func, select
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
    """Return the explicitly configured VPN protocol, or 'invalid'.

    Corrupt/missing credentials must never silently fall back to Outline:
    doing so can route a paid activation through the wrong backend.
    """
    try:
        data = json.loads(decrypt(server.api_secret_encrypted))
        protocol = data.get("protocol")
        return protocol if protocol in ("marzban", "outline") else "invalid"
    except Exception:
        return "invalid"


def _make_client(server: VpnServer) -> AnyClient:
    protocol = _server_protocol(server)
    if protocol == "marzban":
        creds = json.loads(decrypt(server.api_secret_encrypted))
        username = creds.get("username")
        password = creds.get("password")
        if not username or not password:
            raise ValueError("Invalid Marzban credentials")
        return MarzbanClient(
            api_url=server.api_url,
            username=username,
            password=password,
        )
    if protocol == "outline":
        return OutlineClient(api_url=server.api_url)
    raise ValueError(f"VPN server {server.id} has invalid encrypted credentials/protocol")


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
    # Serialize provisioning for the same subscription and node. This closes
    # duplicate webhook/retry races before any remote Marzban call.
    from app.enums import SubscriptionStatus
    from app.models.subscription import Subscription

    locked_sub = (await session.execute(
        select(Subscription).where(
            Subscription.id == subscription_id
        ).with_for_update()
    )).scalar_one_or_none()
    if locked_sub is None or locked_sub.user_id != user_id:
        raise ValueError("Subscription/user mismatch during VPN provisioning")
    if locked_sub.status not in (
        SubscriptionStatus.pending_payment,
        SubscriptionStatus.activation_error,
        SubscriptionStatus.active,
        SubscriptionStatus.expires_soon,
    ):
        raise ValueError("Subscription is not eligible for VPN provisioning")

    locked_server = (await session.execute(
        select(VpnServer).where(VpnServer.id == server.id).with_for_update()
    )).scalar_one_or_none()
    if locked_server is None:
        raise ValueError("VPN server disappeared during provisioning")
    server = locked_server
    if (
        server.status != VpnServerStatus.active
        or not server.is_selectable
        or server.provider == "private-line-primary"
        or _server_protocol(server) != "marzban"
    ):
        raise ValueError("VPN server is not production-selectable")

    actual_users = await session.scalar(
        select(func.count(VpnKey.id)).where(
            VpnKey.server_id == server.id,
            VpnKey.status == VpnKeyStatus.active,
        )
    )
    actual_users = int(actual_users or 0)
    server.current_users = actual_users

    # Idempotent creation: never delete a working key before a replacement exists.
    # Activation retries and queue retries simply reuse the active key on this node.
    existing_active = (await session.execute(
        select(VpnKey).where(
            VpnKey.subscription_id == subscription_id,
            VpnKey.user_id == user_id,
            VpnKey.server_id == server.id,
            VpnKey.status == VpnKeyStatus.active,
        ).order_by(VpnKey.id.desc())
    )).scalars().all()
    if len(existing_active) > 1:
        raise ValueError("Multiple active keys on the same VPN node require reconciliation")
    if existing_active:
        logger.info(
            "Reusing active VPN key %d for subscription %d on server %d",
            existing_active[0].id, subscription_id, server.id,
        )
        return existing_active[0]

    if server.max_users and actual_users >= server.max_users:
        raise ValueError("VPN server capacity exhausted")

    _inherited_reissue_count = 0
    _inherited_reissue_date = None
    _inherited_traffic_gb = 0.0
    client = _make_client(server)
    cb = get_circuit_breaker(server.id)
    protocol = _server_protocol(server)

    try:
        async def _create():
            if protocol == "marzban":
                assert isinstance(client, MarzbanClient)

                # Fail closed BEFORE creating a paid user's remote credential.
                # A healthy API is not sufficient: Xray must be running and
                # Marzban must publish exactly one public host matching the
                # node's API IP and Reality inbound port.
                if not await client.check_connection():
                    raise MarzbanConfigError("Marzban authentication unavailable")
                headers = await client._headers()
                core = await client._client.get(
                    f"{client._base}/api/core", headers=headers
                )
                if core.status_code != 200 or core.json().get("started") is not True:
                    raise MarzbanConfigError("Xray core is not running")
                config = await client._get_core_config()
                reality = await client.get_reality_params()
                if not isinstance(config, dict) or reality is None:
                    raise MarzbanConfigError("Reality configuration unavailable")
                inbounds = [
                    item for item in config.get("inbounds", [])
                    if item.get("protocol") == "vless"
                    and item.get("streamSettings", {}).get("security") == "reality"
                ]
                if len(inbounds) != 1:
                    raise MarzbanConfigError("Expected exactly one VLESS Reality inbound")
                inbound = inbounds[0]
                tag = inbound.get("tag")
                port = inbound.get("port")
                if not tag or not isinstance(port, int) or not (1 <= port <= 65535):
                    raise MarzbanConfigError("Invalid VLESS Reality inbound")
                api_host = urlparse(server.api_url).hostname or ""
                hosts_resp = await client._client.get(
                    f"{client._base}/api/hosts", headers=headers
                )
                if hosts_resp.status_code != 200 or not isinstance(hosts_resp.json(), dict):
                    raise MarzbanConfigError("Marzban public hosts API unavailable")
                entries = hosts_resp.json().get(tag, [])
                valid_hosts = [
                    h for h in entries
                    if isinstance(h, dict)
                    and h.get("address") == api_host
                    and h.get("port") in (None, "", 0, "0", port, str(port))
                    and h.get("is_disabled") not in (True, "true", "True")
                    and h.get("security") in (None, "", "inbound_default", "reality")
                ]
                if len(valid_hosts) != 1:
                    raise MarzbanConfigError(
                        "Marzban does not publish exactly one valid public VLESS host"
                    )

                name = _marzban_username(user_id, subscription_id)
                created = await client.create_key(
                    name=name,
                    traffic_limit_gb=traffic_limit_gb,
                    ip_limit=ip_limit,
                )
                # Validate the exact link before the DB can consider it active.
                from app.services.vpn_link import canonical_client_link
                checked = canonical_client_link(created.access_url, "Private Line VPN")
                parsed_created = urlparse(checked)
                if parsed_created.hostname != api_host or parsed_created.port != port:
                    raise MarzbanConfigError(
                        "Generated VLESS endpoint differs from verified public node endpoint"
                    )
                return created
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

    # Never log raw VLESS URLs (even partial strings include the client's UUID).
    logger.info("VPN access key issued for subscription %d, user %d, server %d",
                subscription_id, user_id, server.id)

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
    # Capacity is owned by this single transactional creation path.
    server.current_users = actual_users + 1
    await session.flush()
    return vpn_key


async def disable_vpn_key(
    vpn_key: VpnKey,
    server: VpnServer,
    session: AsyncSession,
    *,
    enqueue_on_failure: bool = True,
    raise_on_failure: bool = False,
) -> bool:
    """Disable a remote VPN credential and update local state only after confirmation.

    On remote failure the key becomes ERROR (not DISABLED), because the remote
    credential may still work. One retry operation is queued. The pending worker
    calls this with enqueue_on_failure=False and raise_on_failure=True.
    """
    client = _make_client(server)
    try:
        if isinstance(client, MarzbanClient):
            await client.disable_key(vpn_key.outline_key_id)
        else:
            await client.delete_key(vpn_key.outline_key_id)
    except Exception as exc:
        logger.error(
            "Remote VPN disable failed for key %d on server %d: %s",
            vpn_key.id, server.id, type(exc).__name__,
        )
        vpn_key.status = VpnKeyStatus.error
        vpn_key.disabled_at = None
        if enqueue_on_failure:
            try:
                from app.models.pending_operation import PendingOperation, PendingOperationType
                pending = (await session.execute(
                    select(PendingOperation).where(
                        PendingOperation.operation_type == PendingOperationType.disable_key.value,
                        PendingOperation.vpn_key_id == vpn_key.id,
                        PendingOperation.completed_at.is_(None),
                    )
                )).scalars().first()
                if pending is None:
                    session.add(PendingOperation(
                        operation_type=PendingOperationType.disable_key.value,
                        vpn_key_id=vpn_key.id,
                        server_id=server.id,
                        payload={},
                        attempts=0,
                        next_retry_at=datetime.utcnow(),
                    ))
            except Exception as enqueue_exc:
                logger.error(
                    "Failed to queue remote disable retry for key %d: %s",
                    vpn_key.id, type(enqueue_exc).__name__,
                )
        await session.flush()
        if raise_on_failure:
            raise
        return False
    finally:
        await client.aclose()

    vpn_key.status = VpnKeyStatus.disabled
    vpn_key.disabled_at = datetime.utcnow()
    await session.flush()
    return True


async def reissue_vpn_key(
    vpn_key: VpnKey,
    server: VpnServer,
    tariff_slug: str,
    traffic_limit_gb: int | None,
    session: AsyncSession,
) -> VpnKey:
    """Safely refresh a Marzban profile without deleting or rotating credentials.

    Historical behavior deleted the live key first and could strand a paid user
    if the subsequent create failed. Production self-service now preserves the
    issued UUID and Reality credentials. Credential rotation, if ever required,
    must be a separate explicit migration with create-before-cutover semantics.
    """
    if tariff_slug == _START_TARIFF_SLUG and vpn_key.last_reissued_at:
        cooldown_until = vpn_key.last_reissued_at + timedelta(days=_KEY_REISSUE_COOLDOWN_DAYS)
        if datetime.utcnow() < cooldown_until:
            raise ValueError(
                f"Обновление профиля для тарифа «Старт» доступно раз в {_KEY_REISSUE_COOLDOWN_DAYS} дней."
            )
    if _server_protocol(server) != "marzban":
        raise ValueError("Safe profile refresh is supported only for Marzban VLESS nodes")
    from app.services.vpn_reconcile import reconcile_marzban_server
    result = await reconcile_marzban_server(
        session,
        server.id,
        apply=True,
        only_subscription_id=vpn_key.subscription_id,
    )
    if result.errors:
        raise ValueError(f"VPN profile reconciliation failed: {result.errors} integrity error(s)")
    await session.refresh(vpn_key)
    vpn_key.last_reissued_at = datetime.utcnow()
    await session.flush()
    return vpn_key


async def sync_traffic_stats(session: AsyncSession) -> None:
    result = await session.execute(
        select(VpnServer).where(
            VpnServer.status == VpnServerStatus.active,
            VpnServer.is_selectable.is_(True),
            VpnServer.provider != "private-line-primary",
        )
    )
    servers = result.scalars().all()

    for server in servers:
        if _server_protocol(server) != "marzban":
            continue
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
    """Compatibility wrapper around the preserve-first reconciler.

    Kept for older admin/service call sites. It no longer performs independent
    mutation logic or user notifications with raw links.
    """
    from app.services.vpn_reconcile import reconcile_marzban_server

    query = select(VpnServer).where(
        VpnServer.status == VpnServerStatus.active,
        VpnServer.provider != "private-line-primary",
    )
    if server_id is not None:
        query = query.where(VpnServer.id == server_id)
    servers = (await session.execute(query)).scalars().all()

    fixed = 0
    errors = 0
    for server in servers:
        if _server_protocol(server) != "marzban":
            continue
        try:
            result = await reconcile_marzban_server(session, server.id, apply=True)
            fixed += result.repaired_hosts + result.repaired_users + result.repaired_urls
            errors += result.errors
        except Exception:
            logger.exception("Safe Marzban reconciliation failed for server %d", server.id)
            errors += 1
    await session.commit()
    return fixed, errors, []

