from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import PaymentStatus, SubscriptionStatus
from app.models.payment import Payment
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.models.user import User

logger = logging.getLogger(__name__)


async def auto_renew_subscriptions(session: AsyncSession) -> None:
    """Create renewal payments for subscriptions with auto_renew=True expiring in < 24 h."""
    threshold = datetime.utcnow() + timedelta(hours=24)
    result = await session.execute(
        select(Subscription).where(
            Subscription.auto_renew == True,  # noqa: E712
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
            Subscription.expires_at <= threshold,
        )
    )
    subscriptions = result.scalars().all()

    renewed = 0
    for sub in subscriptions:
        # Skip if pending payment already exists for this subscription
        existing = await session.execute(
            select(Payment).where(
                Payment.user_id == sub.user_id,
                Payment.status == PaymentStatus.pending,
            )
        )
        if existing.scalars().first() is not None:
            continue

        tariff = await session.get(Tariff, sub.tariff_id)
        if tariff is None:
            logger.warning("Tariff %d not found for subscription %d", sub.tariff_id, sub.id)
            continue
        user = await session.get(User, sub.user_id)
        if user is None or not user.telegram_id:
            logger.error("Telegram recipient missing for subscription %d", sub.id)
            continue
        telegram_id = user.telegram_id

        try:
            from app.payments.services import create_payment
            payment, payment_result = await create_payment(sub.user_id, tariff.slug, session)
            await session.flush()
            renewed += 1

            # Notify user about the renewal payment
            from app.bot_instance import get_bot
            bot = get_bot()
            if bot is not None:
                text = (
                    "🔄 <b>Автопродление подписки</b>\n\n"
                    f"Тариф: {tariff.name}\n"
                    f"Сумма: {int(payment.amount)} RUB\n\n"
                )
                if payment_result.payment_url:
                    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
                    markup = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="💳 Оплатить", url=payment_result.payment_url)
                    ]])
                    text += "Оплатите для продления доступа:"
                    try:
                        await bot.send_message(
                            chat_id=telegram_id, text=text,
                            parse_mode="HTML", reply_markup=markup,
                        )
                    except Exception as exc:
                        logger.error("Failed to notify user %d about renewal: %s", sub.user_id, exc)
                elif payment_result.display_data.get("stars_flow"):
                    from bot.handlers.stars_payment import send_stars_invoice
                    try:
                        await send_stars_invoice(
                            bot, telegram_id, payment.id, float(payment.amount),
                            f"Private Line — {tariff.name} (автопродление)",
                        )
                    except Exception as exc:
                        logger.error("Failed to send Stars invoice to user %d: %s", sub.user_id, exc)
        except Exception as exc:
            logger.error("Auto-renewal failed for subscription %d: %s", sub.id, exc)

    if renewed:
        await session.commit()
    logger.info("Auto-renewal: created %d payment(s) for %d candidate(s)", renewed, len(subscriptions))
