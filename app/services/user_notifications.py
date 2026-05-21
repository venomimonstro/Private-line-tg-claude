from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def send_key_to_user(user_id: int, vpn_key: object) -> None:
    """Send access key to user after activation."""
    from app.bot_instance import get_bot
    from app.services.vpn_key import get_access_url

    bot = get_bot()
    if bot is None:
        return
    try:
        url = get_access_url(vpn_key)  # type: ignore[arg-type]
        await bot.send_message(
            chat_id=user_id,
            text=(
                "✅ Ваш персональный ключ готов!\n\n"
                f"<code>{url}</code>\n\n"
                "Скопируйте ключ и добавьте в приложение Outline."
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("send_key_to_user failed for user %d: %s", user_id, exc)


async def notify_expiry(user_id: int) -> None:
    """Notify user that subscription expired."""
    from app.bot_instance import get_bot

    bot = get_bot()
    if bot is None:
        return
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "❌ Срок вашего защищённого доступа истёк.\n\n"
                "Нажмите «🔄 Продлить» для возобновления подписки."
            ),
        )
    except Exception as exc:
        logger.error("notify_expiry failed for user %d: %s", user_id, exc)


async def notify_expiring_soon(user_id: int, days: int) -> None:
    """Notify user that subscription expires soon."""
    from app.bot_instance import get_bot

    bot = get_bot()
    if bot is None:
        return
    try:
        day_word = "день" if days == 1 else "дня"
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"⚠️ Ваш защищённый доступ истекает через {days} {day_word}.\n\n"
                "Нажмите «🔄 Продлить», чтобы не потерять доступ."
            ),
        )
    except Exception as exc:
        logger.error("notify_expiring_soon failed for user %d: %s", user_id, exc)
