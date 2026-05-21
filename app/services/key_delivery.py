from __future__ import annotations
import logging
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.bot_instance import get_bot
from app.enums import SubscriptionStatus
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.services.notification import notify_admin

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 5
RETRY_INTERVAL_MINUTES = 15


async def retry_failed_activations(session: AsyncSession) -> None:
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

        vpn_key = await activate_subscription(sub, tariff, session)
        if vpn_key is not None:
            logger.info("Subscription %d activated on retry %d", sub.id, sub.activation_attempts)
            bot = get_bot()
            if bot is not None:
                try:
                    from app.services.vpn_key import get_access_url
                    url = get_access_url(vpn_key)
                    await bot.send_message(
                        chat_id=sub.user_id,
                        text=(
                            "✅ Ваш персональный ключ доступа готов!\n\n"
                            f"<code>{url}</code>\n\n"
                            "Скопируйте ключ и добавьте его в приложение."
                        ),
                        parse_mode="HTML",
                    )
                except Exception as exc:
                    logger.error("Failed to send key to user %s: %s", sub.user_id, exc)
        else:
            sub.last_activation_error = f"Retry {sub.activation_attempts} failed at {datetime.utcnow().isoformat()}"
            if sub.activation_attempts >= _MAX_ATTEMPTS:
                await notify_admin(
                    f"🔴 Подписка #{sub.id} (пользователь {sub.user_id}) "
                    f"не активирована после {_MAX_ATTEMPTS} попыток. "
                    f"Требуется ручное вмешательство."
                )

    await session.commit()
