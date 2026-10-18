"""Subscription availability watchdog with additive failover.

It never rotates or deletes an issued VPN credential. When an active
subscription has no key on a currently healthy/selectable node, the watchdog
may add a replacement profile on another recently-proven node and deliver it
to the user. Existing keys remain intact for later recovery.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

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
            VpnServer.provider != "private-line-primary",
            VpnServer.last_e2e_success_at.is_not(None),
            VpnServer.last_e2e_success_at >= now - timedelta(minutes=10),
            VpnServer.last_hot_add_success_at.is_not(None),
            VpnServer.last_hot_add_success_at >= now - timedelta(minutes=45),
        )
    )).all()
    valid_sub_ids = {row[0] for row in valid_rows}
    missing = [sub for sub in subs if sub.id not in valid_sub_ids]
    results["missing_key"] = len(missing)
    results["ok"] = len(subs) - len(missing)

    if missing:
        from app.models.tariff import Tariff
        from app.models.user import User
        from app.services.subscription import ensure_subscription_vpn_key
        from app.services.user_notifications import send_key_to_user

        for sub in missing:
            try:
                tariff = await session.get(Tariff, sub.tariff_id)
                if tariff is None:
                    raise ValueError("tariff not found")
                key = await ensure_subscription_vpn_key(sub, tariff, session)
                if key is None:
                    raise RuntimeError("no healthy failover node available")
                await session.flush()

                healthy_rows = (await session.execute(
                    select(VpnKey)
                    .join(VpnServer, VpnServer.id == VpnKey.server_id)
                    .where(
                        VpnKey.subscription_id == sub.id,
                        VpnKey.status == VpnKeyStatus.active,
                        VpnServer.status == VpnServerStatus.active,
                        VpnServer.is_selectable.is_(True),
                    )
                    .order_by(VpnKey.id.asc())
                )).scalars().all()
                if not healthy_rows:
                    raise RuntimeError("failover key was not persisted on a healthy node")

                # Persist access before any external notification. A Telegram
                # outage must never roll back a successfully-created remote key.
                await session.commit()
                results["recovered"] += 1
                logger.warning(
                    "watchdog: additive failover restored subscription %d with %d healthy key(s)",
                    sub.id, len(healthy_rows),
                )

                user = await session.get(User, sub.user_id)
                if user is not None and user.telegram_id:
                    try:
                        await send_key_to_user(user.telegram_id, list(healthy_rows))
                    except Exception:
                        logger.error(
                            "watchdog: failover key persisted but Telegram delivery failed "
                            "for subscription %d",
                            sub.id,
                            exc_info=True,
                        )
            except Exception as exc:
                await session.rollback()
                results["recovery_failed"] += 1
                results["errors"].append(
                    f"sub#{sub.id}:{type(exc).__name__}"
                )
                logger.error(
                    "watchdog additive failover failed for subscription %d: %s",
                    sub.id, type(exc).__name__, exc_info=True,
                )

        ids = ", ".join(str(sub.id) for sub in missing[:20])
        message = (
            f"⚠️ VPN watchdog: degraded subscriptions={len(missing)}, "
            f"recovered={results['recovered']}, failed={results['recovery_failed']}.\n"
            f"Sub IDs: {ids}{'...' if len(missing) > 20 else ''}"
        )
        await notify_admin(
            message,
            dedupe_key=(f"subscription-watchdog:{len(missing)}:"
                        f"{results['recovery_failed']}"),
            cooldown_seconds=21600,
        )
        results["ok"] += results["recovered"]
    else:
        logger.info("watchdog: all %d active subscriptions have verified keys", len(subs))
    return results


async def _find_best_server(session: AsyncSession) -> VpnServer | None:
    """Return the best currently admitted node for diagnostics/recovery."""
    from app.services.circuit_breaker import CircuitState, get_circuit_breaker
    from app.services.vpn_key import _server_protocol

    servers = (await session.execute(
        select(VpnServer)
        .where(
            VpnServer.status == VpnServerStatus.active,
            VpnServer.is_selectable.is_(True),
            VpnServer.provider != "private-line-primary",
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
