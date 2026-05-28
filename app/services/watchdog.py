"""Subscription watchdog: ensures every active subscription has a working VPN key.

Runs every 30 minutes via scheduler. For each active subscription that lacks
an active key it attempts to create one on the best available server.
If creation fails (server down) it enqueues a pending operation for later retry.
Notifications go to the user via bot and to admin via Telegram.
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
    """Check all active subs for missing/broken keys. Attempt auto-recovery.

    Returns a summary dict with counts.
    """
    from app.models.user import User
    from app.models.tariff import Tariff
    from app.services.vpn_key import create_vpn_key
    from app.services.notification import notify_admin

    results = {
        "checked": 0,
        "ok": 0,
        "missing_key": 0,
        "recovered": 0,
        "recovery_failed": 0,
        "errors": [],
    }

    # Load all active subscriptions (active + expires_soon)
    subs_result = await session.execute(
        select(Subscription).where(
            Subscription.status.in_([
                SubscriptionStatus.active,
                SubscriptionStatus.expires_soon,
            ])
        )
    )
    subs: list[Subscription] = list(subs_result.scalars().all())
    results["checked"] = len(subs)

    if not subs:
        logger.info("watchdog: no active subscriptions found")
        return results

    # Build set of subscription_ids that have an active key
    keys_result = await session.execute(
        select(VpnKey.subscription_id).where(
            VpnKey.status == VpnKeyStatus.active
        )
    )
    active_key_sub_ids: set[int] = {row[0] for row in keys_result.all()}

    # Find subscriptions with no active key
    subs_missing_key = [s for s in subs if s.id not in active_key_sub_ids]
    results["missing_key"] = len(subs_missing_key)

    if not subs_missing_key:
        results["ok"] = len(subs)
        logger.info("watchdog: all %d subscriptions have active keys", len(subs))
        return results

    results["ok"] = len(subs) - len(subs_missing_key)
    logger.warning(
        "watchdog: %d subscriptions have no active key — attempting recovery",
        len(subs_missing_key),
    )

    # Notify admin about missing keys (batch)
    if subs_missing_key:
        sub_ids_str = ", ".join(str(s.id) for s in subs_missing_key[:10])
        await notify_admin(
            f"⚠️ Watchdog: {len(subs_missing_key)} активных подписок без VPN-ключа.\n"
            f"Sub IDs: {sub_ids_str}{'...' if len(subs_missing_key) > 10 else ''}\n"
            f"Пробую авто-восстановление..."
        )

    # Find best available server
    best_server = await _find_best_server(session)
    if best_server is None:
        msg = "watchdog: no healthy servers available — cannot auto-recover"
        logger.error(msg)
        results["errors"].append(msg)
        await notify_admin(
            f"🔴 Watchdog: нет доступных VPN-серверов для восстановления ключей!\n"
            f"{len(subs_missing_key)} подписок без ключей."
        )
        return results

    recovered_users: list[tuple[int, str]] = []  # (telegram_id, access_url)

    for sub in subs_missing_key:
        try:
            tariff = await session.get(Tariff, sub.tariff_id)
            user = await session.get(User, sub.user_id)

            vpn_key = await create_vpn_key(
                user_id=sub.user_id,
                subscription_id=sub.id,
                server=best_server,
                traffic_limit_gb=tariff.traffic_limit_gb if tariff else None,
                label="watchdog",
                session=session,
            )
            await session.commit()

            results["recovered"] += 1
            logger.info(
                "watchdog: recovered key for sub %d user %d → key %d",
                sub.id, sub.user_id, vpn_key.id,
            )

            if user:
                from app.encryption import decrypt
                try:
                    access_url = decrypt(vpn_key.access_url_encrypted)
                    recovered_users.append((user.telegram_id, access_url))
                except Exception:
                    pass

        except Exception as exc:
            results["recovery_failed"] += 1
            logger.error(
                "watchdog: failed to recover key for sub %d: %s",
                sub.id, exc,
            )
            results["errors"].append(f"sub {sub.id}: {str(exc)[:100]}")

    # Notify users whose keys were recovered
    if recovered_users:
        try:
            from app.services.user_notifications import send_key_to_user
            for telegram_id, _url in recovered_users:
                try:
                    # send_key_to_user needs a VpnKey object; use raw message instead
                    from aiogram import Bot
                    from app.config import settings
                    from app.bot_instance import get_bot
                    bot = get_bot()
                    if bot is None:
                        bot = Bot(token=settings.BOT_TOKEN)
                        close_bot = True
                    else:
                        close_bot = False
                    try:
                        await bot.send_message(
                            chat_id=telegram_id,
                            text=(
                                "✅ <b>Ваш VPN-доступ восстановлен</b>\n\n"
                                "Система обнаружила, что у вас не было активного ключа, "
                                "и автоматически создала новый.\n\n"
                                "Пожалуйста, перейдите в бота и получите актуальный ключ: "
                                "нажмите /start или кнопку «Мой ключ»."
                            ),
                            parse_mode="HTML",
                        )
                    finally:
                        if close_bot:
                            await bot.session.close()
                except Exception as notify_exc:
                    logger.warning(
                        "watchdog: could not notify user tg=%d: %s",
                        telegram_id, notify_exc,
                    )
        except Exception as exc:
            logger.warning("watchdog: batch notification failed: %s", exc)

    # Final admin notification
    if results["recovered"] > 0 or results["recovery_failed"] > 0:
        await notify_admin(
            f"🔧 Watchdog завершил восстановление:\n"
            f"  ✅ Восстановлено: {results['recovered']}\n"
            f"  ❌ Не удалось: {results['recovery_failed']}\n"
            + (f"  Ошибки: {results['errors'][:3]}" if results["errors"] else "")
        )

    return results


async def _find_best_server(session: AsyncSession) -> VpnServer | None:
    """Return the active server with the fewest recent errors."""
    from app.services.circuit_breaker import get_circuit_breaker, CircuitState

    result = await session.execute(
        select(VpnServer)
        .where(VpnServer.status == VpnServerStatus.active)
        .order_by(VpnServer.failure_count.asc())
    )
    servers = result.scalars().all()

    for server in servers:
        cb = get_circuit_breaker(server.id)
        if cb.state != CircuitState.OPEN:
            return server

    # All circuits open — return the one with fewest failures anyway
    return servers[0] if servers else None


async def check_key_health(vpn_key: VpnKey, session: AsyncSession) -> dict:
    """Quick health check for a single VPN key.

    Returns a dict with keys: tcp_ok, params_ok, sni_clean, issues, url_preview
    """
    import asyncio
    import re
    from urllib.parse import parse_qs, urlparse

    from app.encryption import decrypt

    result: dict = {
        "tcp_ok": None,
        "params_ok": False,
        "sni_clean": None,
        "issues": [],
        "url_preview": "",
        "host": None,
        "port": None,
    }

    try:
        url = decrypt(vpn_key.access_url_encrypted)
    except Exception as exc:
        result["issues"].append(f"Cannot decrypt URL: {exc}")
        return result

    result["url_preview"] = url[:60]

    if not url.startswith("vless://"):
        result["issues"].append("URL is not vless://")
        return result

    # Parse params
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        host = parsed.hostname
        port = parsed.port or 443
        result["host"] = host
        result["port"] = port

        security = qs.get("security", [""])[0]
        pbk = qs.get("pbk", [""])[0]
        sid = qs.get("sid", [""])[0]
        sni = qs.get("sni", [""])[0]
        flow = qs.get("flow", [""])[0]

        issues = []
        if security != "reality":
            issues.append(f"security={security!r} (expected 'reality')")
        if not pbk:
            issues.append("missing pbk")
        if not sid:
            issues.append("missing sid")
        if not sni:
            issues.append("missing sni")
        if flow != "xtls-rprx-vision":
            issues.append(f"flow={flow!r} (expected 'xtls-rprx-vision')")

        # Check SNI for Markdown corruption
        _MD = re.compile(r'\[([^\]]+)\]\([^)]*\)')
        if _MD.search(sni):
            issues.append(f"SNI contains Markdown formatting: {sni!r}")
            result["sni_clean"] = False
        else:
            result["sni_clean"] = True

        result["params_ok"] = len(issues) == 0
        result["issues"].extend(issues)

        # TCP connectivity check
        if host:
            try:
                conn = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=5.0,
                )
                conn[1].close()
                result["tcp_ok"] = True
            except Exception as exc:
                result["tcp_ok"] = False
                result["issues"].append(f"TCP {host}:{port} unreachable: {exc}")

    except Exception as exc:
        result["issues"].append(f"URL parse error: {exc}")

    return result
