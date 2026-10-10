"""Fail-closed reconciliation for a Marzban VLESS Reality node.

This module preserves existing client UUIDs and the node's Reality private key.
It repairs only:
- Marzban public host metadata;
- missing users when the original UUID is known;
- flow/inbound assignment for an existing matching UUID;
- the encrypted client URL in the control-plane DB.

It never deletes users, rotates UUIDs, rotates Reality keys, or rewrites Xray's
static Reality configuration.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.encryption import encrypt
from app.enums import SubscriptionStatus, VpnKeyStatus, VpnServerStatus
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _make_client, _server_protocol, _marzban_username, get_access_url
from app.services.vpn_link import canonical_client_link
from app.vpn.marzban import MarzbanConfigError

logger = logging.getLogger(__name__)


@dataclass
class ReconcileResult:
    server_id: int
    checked_keys: int = 0
    repaired_users: int = 0
    repaired_urls: int = 0
    repaired_hosts: int = 0
    errors: int = 0
    core_restarted: bool = False


def _public_api_host(server: VpnServer) -> str:
    host = urlsplit(server.api_url).hostname or ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise MarzbanConfigError("VPN node API URL must contain a public IP") from exc
    if not ip.is_global:
        raise MarzbanConfigError("VPN node API IP is not globally routable")
    return str(ip)


def _raw_reality_link(
    *, uuid: str, host: str, port: int, public_key: str, short_id: str, sni: str
) -> str:
    query = urlencode({
        "encryption": "none",
        "security": "reality",
        "sni": sni,
        "fp": "chrome",
        "pbk": public_key,
        "sid": short_id,
        "type": "tcp",
        "headerType": "none",
        "flow": "xtls-rprx-vision",
    })
    return urlunsplit(("vless", f"{uuid}@{host}:{port}", "", query, "Private%20Line%20VPN"))


async def reconcile_marzban_server(
    session: AsyncSession,
    server_id: int,
    *,
    apply: bool,
    only_subscription_id: int | None = None,
) -> ReconcileResult:
    server = await session.get(VpnServer, server_id)
    if server is None:
        raise MarzbanConfigError("VPN server not found")
    if _server_protocol(server) != "marzban":
        raise MarzbanConfigError("VPN server is not Marzban")
    if server.provider == "private-line-primary":
        raise MarzbanConfigError("Control-plane VPS is not an allowed VPN node")

    public_host = _public_api_host(server)
    result = ReconcileResult(server_id=server.id)
    client = _make_client(server)
    remote_changed = False
    try:
        if not await client.check_connection():
            raise MarzbanConfigError("Marzban API authentication failed")
        headers = await client._headers()
        config = await client._get_core_config()
        reality = await client.get_reality_params()
        if not isinstance(config, dict) or reality is None:
            raise MarzbanConfigError("Xray Reality configuration unavailable")

        reality_inbounds = [
            item for item in config.get("inbounds", [])
            if item.get("protocol") == "vless"
            and item.get("streamSettings", {}).get("security") == "reality"
        ]
        if len(reality_inbounds) != 1:
            raise MarzbanConfigError("Expected exactly one VLESS Reality inbound")
        inbound = reality_inbounds[0]
        tag = inbound.get("tag")
        port = inbound.get("port")
        if not tag or not isinstance(port, int) or not (1 <= port <= 65535):
            raise MarzbanConfigError("Invalid VLESS Reality inbound tag/port")
        public_key, short_id, sni = reality
        if not short_id:
            raise MarzbanConfigError("Reality shortId is empty")

        core = await client._client.get(f"{client._base}/api/core", headers=headers)
        if core.status_code != 200 or core.json().get("started") is not True:
            raise MarzbanConfigError("Xray core is not running")

        # Normalise Marzban's public host metadata. This does not rotate any key.
        hosts_response = await client._client.get(
            f"{client._base}/api/hosts", headers=headers
        )
        if hosts_response.status_code != 200 or not isinstance(hosts_response.json(), dict):
            raise MarzbanConfigError("Marzban hosts API unavailable")
        host_map = hosts_response.json()
        current_entries = host_map.get(tag, [])
        expected_host = {
            "remark": "Private Line VPN",
            "address": public_host,
            "port": port,
            "sni": sni,
            "host": "",
            "path": "",
            "security": "inbound_default",
            "alpn": "",
            "fingerprint": "chrome",
            "allowinsecure": False,
            "is_disabled": False,
            "mux_enable": False,
            "fragment_setting": "",
            "noise_setting": "",
            "random_user_agent": False,
        }
        hosts_ok = (
            isinstance(current_entries, list)
            and len(current_entries) == 1
            and isinstance(current_entries[0], dict)
            and current_entries[0].get("address") == public_host
            and current_entries[0].get("port") in (port, str(port))
            and current_entries[0].get("is_disabled") not in (True, "true", "True")
            and current_entries[0].get("fingerprint") in ("chrome", None, "")
            and current_entries[0].get("security") in ("inbound_default", None, "")
        )
        if not hosts_ok:
            if apply:
                base = current_entries[0] if (
                    isinstance(current_entries, list)
                    and current_entries
                    and isinstance(current_entries[0], dict)
                ) else {}
                host_map[tag] = [{**base, **expected_host}]
                saved = await client._client.put(
                    f"{client._base}/api/hosts",
                    headers={**headers, "Content-Type": "application/json"},
                    json=host_map,
                )
                if saved.status_code != 200:
                    raise MarzbanConfigError(
                        f"Could not repair Marzban public host (HTTP {saved.status_code})"
                    )
                result.repaired_hosts += 1
            else:
                result.errors += 1

        key_query = select(VpnKey, Subscription).join(
            Subscription, Subscription.id == VpnKey.subscription_id
        ).where(
            VpnKey.server_id == server.id,
            VpnKey.status == VpnKeyStatus.active,
            Subscription.status.in_([
                SubscriptionStatus.active,
                SubscriptionStatus.expires_soon,
            ]),
            Subscription.expires_at > datetime.utcnow(),
        )
        if only_subscription_id is not None:
            key_query = key_query.where(VpnKey.subscription_id == only_subscription_id)
        rows = (await session.execute(key_query)).all()

        for key, subscription in rows:
            result.checked_keys += 1
            try:
                old = get_access_url(key)
                parsed = urlsplit(old)
                original_uuid = str(UUID(parsed.username or ""))
                if subscription.user_id != key.user_id:
                    raise MarzbanConfigError("VPN key owner/subscription mismatch")

                user_response = await client._client.get(
                    f"{client._base}/api/user/{key.outline_key_id}",
                    headers=headers,
                )
                current = None
                if user_response.status_code == 200:
                    current = user_response.json()
                    expected_username = _marzban_username(key.user_id, key.subscription_id)
                    if key.outline_key_id != expected_username:
                        raise MarzbanConfigError(
                            "Stored Marzban username does not match subscription ownership"
                        )

                    remote_uuid = current.get("proxies", {}).get("vless", {}).get("id")
                    try:
                        remote_uuid = str(UUID(remote_uuid or ""))
                    except ValueError as exc:
                        raise MarzbanConfigError("Marzban user has invalid UUID") from exc

                    remote_status = current.get("status")
                    remote_limit = current.get("data_limit") or 0
                    remote_used = current.get("used_traffic") or 0
                    if remote_status == "limited" and remote_limit and remote_used >= remote_limit:
                        # Quota exhaustion is intentional and must never be undone.
                        needs_remote_repair = False
                    else:
                        assigned = current.get("inbounds", {}).get("vless", [])
                        flow = current.get("proxies", {}).get("vless", {}).get("flow")
                        needs_remote_repair = (
                            remote_uuid != original_uuid
                            or remote_status != "active"
                            or tag not in assigned
                            or flow != "xtls-rprx-vision"
                        )

                    if needs_remote_repair:
                        if not apply:
                            result.errors += 1
                        else:
                            # The control-plane DB stores the credential already
                            # issued to the customer. Restore Marzban to that UUID;
                            # never force the customer to rotate because panel
                            # state drifted.
                            update = {
                                "proxies": {
                                    **current.get("proxies", {}),
                                    "vless": {
                                        **current.get("proxies", {}).get("vless", {}),
                                        "id": original_uuid,
                                        "flow": "xtls-rprx-vision",
                                    },
                                },
                                "inbounds": {
                                    **current.get("inbounds", {}),
                                    "vless": [tag],
                                },
                                "status": "active",
                            }
                            repaired = await client._client.put(
                                f"{client._base}/api/user/{key.outline_key_id}",
                                headers=headers,
                                json=update,
                            )
                            if repaired.status_code not in (200, 201):
                                raise MarzbanConfigError(
                                    f"Marzban user repair HTTP {repaired.status_code}"
                                )
                            verify_user = await client._client.get(
                                f"{client._base}/api/user/{key.outline_key_id}",
                                headers=headers,
                            )
                            if verify_user.status_code != 200:
                                raise MarzbanConfigError("Cannot verify repaired Marzban user")
                            verified = verify_user.json()
                            if (
                                verified.get("proxies", {}).get("vless", {}).get("id") != original_uuid
                                or verified.get("proxies", {}).get("vless", {}).get("flow") != "xtls-rprx-vision"
                                or tag not in verified.get("inbounds", {}).get("vless", [])
                                or verified.get("status") != "active"
                            ):
                                raise MarzbanConfigError(
                                    "Marzban user repair did not persist exact issued credential"
                                )
                            result.repaired_users += 1
                            remote_changed = True
                elif user_response.status_code == 404:
                    if not apply:
                        result.errors += 1
                    else:
                        tariff = await session.get(Tariff, subscription.tariff_id)
                        traffic_limit = key.traffic_limit_gb
                        if traffic_limit is None and tariff is not None:
                            traffic_limit = tariff.traffic_limit_gb
                        used = max(0.0, key.traffic_used_gb or 0.0)
                        remaining = (
                            max(0.0, float(traffic_limit) - used)
                            if traffic_limit and traffic_limit > 0 else None
                        )
                        ip_limit = int(getattr(tariff, "max_devices", 0) or 0) if tariff else 0
                        recreated = await client.create_key(
                            name=key.outline_key_id,
                            traffic_limit_gb=remaining,
                            ip_limit=ip_limit,
                            uuid=original_uuid,
                        )
                        if str(UUID(urlsplit(recreated.access_url).username or "")) != original_uuid:
                            raise MarzbanConfigError("Recreated Marzban user changed UUID")
                        result.repaired_users += 1
                        remote_changed = True
                else:
                    raise MarzbanConfigError(
                        f"Marzban user lookup HTTP {user_response.status_code}"
                    )

                corrected = canonical_client_link(
                    _raw_reality_link(
                        uuid=original_uuid,
                        host=public_host,
                        port=port,
                        public_key=public_key,
                        short_id=short_id,
                        sni=sni,
                    ),
                    "Private Line VPN",
                )
                if canonical_client_link(old, "Private Line VPN") != corrected:
                    if apply:
                        key.access_url_encrypted = encrypt(corrected)
                        result.repaired_urls += 1
                    else:
                        result.errors += 1
            except Exception as exc:
                logger.error(
                    "VPN reconcile key=%d server=%d failed: %s: %s",
                    key.id, server.id, type(exc).__name__, str(exc)[:180],
                )
                result.errors += 1

        if apply and remote_changed:
            restarted = await client._client.post(
                f"{client._base}/api/core/restart",
                headers=headers,
                timeout=60.0,
            )
            if restarted.status_code not in (200, 204):
                raise MarzbanConfigError(
                    f"Xray core resync failed HTTP {restarted.status_code}"
                )
            # Do not report repaired until the restarted core is actually ready.
            ready = False
            for _ in range(30):
                await asyncio.sleep(1)
                try:
                    status = await client._client.get(
                        f"{client._base}/api/core",
                        headers=headers,
                        timeout=4.0,
                    )
                    if status.status_code == 200 and status.json().get("started") is True:
                        ready = True
                        break
                except Exception:
                    continue
            if not ready:
                raise MarzbanConfigError("Xray core did not return to started state after resync")
            result.core_restarted = True

        if apply and result.errors == 0:
            # Reconciliation repairs state but never publishes a staging node.
            # Publication requires disposable-user VLESS/HTTPS smoke success.
            server.status = VpnServerStatus.active
            server.failure_count = 0
            server.last_error = None
            server.last_error_at = None
        await session.flush()
        return result
    finally:
        await client.aclose()
