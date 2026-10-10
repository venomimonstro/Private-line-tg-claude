from __future__ import annotations

import logging
from html import escape
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_instance import get_bot
from app.enums import SubscriptionStatus
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.models.key_delivery_log import KeyDeliveryLog
from app.services.notification import notify_admin

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 5
RETRY_INTERVAL_MINUTES = 15


async def _log_delivery(
    session: AsyncSession,
    user_id: int,
    subscription_id: int,
    vpn_key_id: int | None,
    attempt_number: int,
    delivered_at: datetime | None,
    error_msg: str | None = None,
) -> None:
    """Create a KeyDeliveryLog entry for tracking delivery attempts."""
    log = KeyDeliveryLog(
        user_id=user_id,
        subscription_id=subscription_id,
        vpn_key_id=vpn_key_id,
        delivered_at=delivered_at,
        attempt_number=attempt_number,
        error_msg=error_msg,
        created_at=datetime.utcnow(),
    )
    session.add(log)
    try:
        await session.flush()
    except Exception as exc:
        logger.error("Failed to save KeyDeliveryLog: %s", exc)


async def retry_failed_activations(
    session: AsyncSession,
    send_key_callback: object | None = None,
) -> None:
    """Retry subscriptions that failed to activate. Called by scheduler."""
    result = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.tariff))
        .where(
            Subscription.status == SubscriptionStatus.activation_error,
            Subscription.activation_attempts < _MAX_ATTEMPTS,
        )
    )
    subscriptions = result.scalars().all()

    for sub in subscriptions:
        tariff: Tariff | None = sub.tariff
        if tariff is None:
            logger.error("Tariff %d not found for subscription %d", sub.tariff_id, sub.id)
            continue

        from app.services.subscription import activate_subscription
        logger.info(
            "Retrying activation for subscription %d (attempt %d/%d)",
            sub.id, sub.activation_attempts + 1, _MAX_ATTEMPTS,
        )

        vpn_key = await activate_subscription(sub, tariff, session, send_key_callback=send_key_callback)
        if vpn_key is not None:
            logger.info("Subscription %d activated on retry %d", sub.id, sub.activation_attempts)
            bot = get_bot()
            if send_key_callback is None and bot is not None:
                try:
                    from app.services.vpn_key import get_client_access_url
                    url = get_client_access_url(vpn_key)
                    from app.models.user import User
                    user = await session.get(User, sub.user_id)
                    if user is None or not user.telegram_id:
                        raise RuntimeError(f"Telegram ID missing for user {sub.user_id}")
                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=(
                            "✅ Ваш персональный ключ доступа готов!\n\n"
                            f"<code>{escape(url)}</code>\n\n"
                            "Скопируйте ключ и добавьте его в приложение."
                        ),
                        parse_mode="HTML",
                    )
                    await _log_delivery(
                        session=session,
                        user_id=sub.user_id,
                        subscription_id=sub.id,
                        vpn_key_id=vpn_key.id,
                        attempt_number=sub.activation_attempts,
                        delivered_at=datetime.utcnow(),
                    )
                except Exception as exc:
                    logger.error("Failed to send key to user %s: %s", sub.user_id, exc)
                    await _log_delivery(
                        session=session,
                        user_id=sub.user_id,
                        subscription_id=sub.id,
                        vpn_key_id=vpn_key.id,
                        attempt_number=sub.activation_attempts,
                        delivered_at=None,
                        error_msg=str(exc)[:500],
                    )
        else:
            sub.last_activation_error = f"Retry {sub.activation_attempts} failed at {datetime.utcnow().isoformat()}"
            if sub.activation_attempts >= _MAX_ATTEMPTS:
                await notify_admin(
                    f"🔴 Подписка #{sub.id} (пользователь {sub.user_id}) "
                    f"не активирована после {_MAX_ATTEMPTS} попыток. "
                    f"Требуется ручное вмешательство."
                )
            await _log_delivery(
                session=session,
                user_id=sub.user_id,
                subscription_id=sub.id,
                vpn_key_id=None,
                attempt_number=sub.activation_attempts,
                delivered_at=None,
                error_msg=sub.last_activation_error,
            )

    await session.commit()
