from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.enums import SubscriptionStatus
from app.models.subscription import Subscription
from app.models.subscription_notification import SubscriptionNotification
from app.models.tariff import Tariff
from app.models.user import User

logger = logging.getLogger(__name__)

_WINDOW = timedelta(minutes=16)  # ±16 min around target (scheduler runs every 30 min)

_ACTIVE = [SubscriptionStatus.active.value, SubscriptionStatus.expires_soon.value]
_EXPIRED = [SubscriptionStatus.expired.value, SubscriptionStatus.disabled.value]

_CHECKS: list[tuple[str, list[str], timedelta]] = [
    ("24h",     _ACTIVE,          timedelta(hours=24)),
    ("12h",     _ACTIVE,          timedelta(hours=12)),
    ("expiry",  _ACTIVE + _EXPIRED, timedelta(0)),
    ("post24h", _EXPIRED,         timedelta(hours=-24)),
]


async def check_expiry_reminders(session: AsyncSession) -> None:
    now = datetime.utcnow()

    for notif_type, statuses, offset in _CHECKS:
        target = now + offset
        window_start = target - _WINDOW
        window_end = target + _WINDOW

        already_sent = select(SubscriptionNotification.subscription_id).where(
            SubscriptionNotification.notification_type == notif_type
        )

        result = await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
            .where(
                Subscription.expires_at >= window_start,
                Subscription.expires_at < window_end,
                Subscription.status.in_(statuses),
                Subscription.id.not_in(already_sent),
            )
        )
        subs = result.scalars().all()

        for sub in subs:
            if not sub.user or not sub.user.telegram_id:
                continue
            try:
                await _send_notification(sub, notif_type, now)
                session.add(SubscriptionNotification(
                    subscription_id=sub.id,
                    notification_type=notif_type,
                ))
            except Exception as exc:
                logger.error("expiry reminder %s sub %d: %s", notif_type, sub.id, exc)

    await session.commit()


async def _send_notification(sub: Subscription, notif_type: str, now: datetime) -> None:
    from app.bot_instance import get_bot
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    bot = get_bot()
    if bot is None:
        return

    price = int(sub.tariff.price_monthly) if sub.tariff else 0
    btn_label = f"💳 Продлить за {price} ₽" if price else "💳 Продлить"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_label, callback_data="back_to_plans")]
    ])

    if notif_type == "24h":
        text = (
            "⏰ <b>До окончания подписки — 24 часа.</b>\n\n"
            "Не потеряйте доступ — продлите сейчас."
        )
    elif notif_type == "12h":
        text = (
            "⚠️ <b>До окончания подписки — 12 часов!</b>\n\n"
            "Продлите заранее, чтобы не прерывалась работа."
        )
    elif notif_type == "expiry":
        text = (
            "❌ <b>Ваша подписка истекла.</b>\n\n"
            "Продлите доступ, чтобы продолжить использование VPN."
        )
    else:  # post24h
        text = (
            "🔁 <b>Прошли сутки с момента окончания подписки.</b>\n\n"
            "Возобновите доступ в пару кликов."
        )

    await bot.send_message(
        chat_id=sub.user.telegram_id,
        text=text,
        parse_mode="HTML",
        reply_markup=kb,
    )
