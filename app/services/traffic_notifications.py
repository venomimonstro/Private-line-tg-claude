from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import VpnKeyStatus

logger = logging.getLogger(__name__)

_THRESHOLDS = [80, 95]  # percent


async def check_traffic_limits(session: AsyncSession) -> None:
    from app.models.vpn_key import VpnKey
    from app.models.user import User

    result = await session.execute(
        select(VpnKey).where(
            VpnKey.status == VpnKeyStatus.active,
            VpnKey.traffic_limit_gb.is_not(None),
        )
    )
    keys = result.scalars().all()

    notifications_sent = 0
    for key in keys:
        if key.traffic_limit_gb is None or key.traffic_limit_gb <= 0:
            continue

        percent = (key.traffic_used_gb / key.traffic_limit_gb) * 100.0
        gb_remaining = key.traffic_limit_gb - key.traffic_used_gb

        user = await session.get(User, key.user_id)
        if user is None or not user.telegram_id:
            continue
        if percent >= 95 and not key.notified_95pct:
            await notify_traffic_threshold(user.telegram_id, 95, max(0.0, gb_remaining))
            key.notified_95pct = True
            notifications_sent += 1
        elif percent >= 80 and not key.notified_80pct:
            await notify_traffic_threshold(user.telegram_id, 80, max(0.0, gb_remaining))
            key.notified_80pct = True
            notifications_sent += 1

    if notifications_sent > 0:
        await session.commit()
        logger.info("Sent %d traffic threshold notifications", notifications_sent)
    else:
        logger.debug("No traffic threshold notifications needed")


async def notify_traffic_threshold(telegram_id: int, percent: int, gb_remaining: float) -> None:
    """Send Telegram notification for traffic threshold."""
    from app.bot_instance import get_bot

    bot = get_bot()
    if bot is None:
        logger.warning("Bot not available for traffic notification to Telegram %d", telegram_id)
        return

    if percent >= 95:
        text = (
            "🔴 <b>Внимание! Трафик почти исчерпан</b>\n\n"
            f"Вы использовали <b>{percent}%</b> месячного лимита трафика.\n"
            f"Осталось: <b>{gb_remaining:.2f} ГБ</b>.\n\n"
            "После исчерпания лимита VPN-доступ будет приостановлен до следующего периода.\n"
            "Рассмотрите переход на тариф с большим объёмом трафика."
        )
    else:
        text = (
            "⚠️ <b>80% трафика использовано</b>\n\n"
            f"Вы использовали <b>{percent}%</b> месячного лимита трафика.\n"
            f"Осталось: <b>{gb_remaining:.2f} ГБ</b>.\n\n"
            "Следите за расходом, чтобы не потерять доступ раньше окончания подписки."
        )

    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=text,
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("notify_traffic_threshold failed for Telegram %d: %s", telegram_id, exc)
