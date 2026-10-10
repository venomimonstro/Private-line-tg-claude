"""Read-only subscription watchdog.

Production rule: a background scheduler may detect and alert, but must never
mint/rotate/reissue VPN credentials on its own. Recovery is performed by the
explicit preserve-first fleet stabilizer or by a concrete pending activation
operation created at payment time.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import SubscriptionStatus, VpnKeyStatus, VpnServerStatus
from app.models.subscription import Subscription
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer

logger = logging.getLogger(__name__)


async def run_subscription_watchdog(session: AsyncSession) -> dict:
    from app.services.notification import notify_admin

    results = {
        "checked": 0,
        "ok": 0,
        "missing_key": 0,
        "recovered": 0,
        "recovery_failed": 0,
        "errors": [],
    }
    now = datetime.utcnow()
    subs = list((await session.execute(
        select(Subscription).where(
            Subscription.status.in_([
                SubscriptionStatus.active,
                SubscriptionStatus.expires_soon,
            ]),
            Subscription.expires_at > now,
        )
    )).scalars().all())
    results["checked"] = len(subs)
    if not subs:
        return results

    valid_rows = (await session.execute(
        select(VpnKey.subscription_id)
        .join(VpnServer, VpnServer.id == VpnKey.server_id)
        .where(
            VpnKey.status == VpnKeyStatus.active,
            VpnServer.status == VpnServerStatus.active,
            VpnServer.is_selectable.is_(True),
            VpnServer.provider == "marzban",
        )
    )).all()
    valid_sub_ids = {row[0] for row in valid_rows}
    missing = [sub for sub in subs if sub.id not in valid_sub_ids]
    results["missing_key"] = len(missing)
    results["ok"] = len(subs) - len(missing)

    if missing:
        ids = ", ".join(str(sub.id) for sub in missing[:20])
        message = (
            f"🔴 VPN watchdog: {len(missing)} активных подписок без verified Marzban key.\n"
            f"Sub IDs: {ids}{'...' if len(missing) > 20 else ''}\n"
            "Автоматический перевыпуск отключён. Запустите production stabilization."
        )
        logger.error("watchdog: %s", message.replace("\n", " | "))
        await notify_admin(message)
    else:
        logger.info("watchdog: all %d active subscriptions have verified keys", len(subs))
    return results


async def _find_best_server(session: AsyncSession) -> VpnServer | None:
    """Read-only helper retained for diagnostics; never creates credentials."""
    from app.services.circuit_breaker import CircuitState, get_circuit_breaker
    from app.services.vpn_key import _server_protocol

    servers = (await session.execute(
        select(VpnServer)
        .where(
            VpnServer.status == VpnServerStatus.active,
            VpnServer.is_selectable.is_(True),
            VpnServer.provider == "marzban",
        )
        .order_by(VpnServer.failure_count.asc(), VpnServer.current_users.asc())
    )).scalars().all()
    for server in servers:
        if _server_protocol(server) != "marzban":
            continue
        if server.max_users and server.current_users >= server.max_users:
            continue
        if get_circuit_breaker(server.id).state == CircuitState.OPEN:
            continue
        return server
    return None


async def check_key_health(vpn_key: VpnKey, session: AsyncSession) -> dict:
    """Read-only syntactic/TCP key check. It never mutates the user or node."""
    import asyncio
    import re
    from urllib.parse import parse_qs, urlparse
    from app.encryption import decrypt

    result: dict = {
        "tcp_ok": None,
        "params_ok": False,
        "sni_clean": None,
        "issues": [],
        "url_preview": "(redacted)",
        "host": None,
        "port": None,
    }
    try:
        url = decrypt(vpn_key.access_url_encrypted)
    except Exception as exc:
        result["issues"].append(f"Cannot decrypt URL: {type(exc).__name__}")
        return result
    if not url.startswith("vless://"):
        result["issues"].append("URL is not vless://")
        return result
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        host = parsed.hostname
        port = parsed.port or 443
        result["host"], result["port"] = host, port
        security = qs.get("security", [""])[0]
        pbk = qs.get("pbk", [""])[0]
        sid = qs.get("sid", [""])[0]
        sni = qs.get("sni", [""])[0]
        flow = qs.get("flow", [""])[0]
        issues = []
        if security != "reality": issues.append("security is not reality")
        if not pbk: issues.append("missing pbk")
        if not sid: issues.append("missing sid")
        if not sni: issues.append("missing sni")
        if flow != "xtls-rprx-vision": issues.append("flow is not xtls-rprx-vision")
        if re.search(r'\[[^\]]+\]\([^)]*\)', sni):
            issues.append("SNI contains Markdown formatting")
            result["sni_clean"] = False
        else:
            result["sni_clean"] = True
        result["params_ok"] = not issues
        result["issues"].extend(issues)
        if host:
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=5.0
                )
                writer.close()
                await writer.wait_closed()
                result["tcp_ok"] = True
            except Exception as exc:
                result["tcp_ok"] = False
                result["issues"].append(f"TCP endpoint unreachable: {type(exc).__name__}")
    except Exception as exc:
        result["issues"].append(f"URL parse error: {type(exc).__name__}")
    return result
