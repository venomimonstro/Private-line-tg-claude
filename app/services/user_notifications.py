from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def send_key_to_user(user_id: int, vpn_keys: object) -> None:
    """Send all access keys to user after activation, then send onboarding."""
    from app.bot_instance import get_bot
    from app.services.vpn_key import get_access_url

    bot = get_bot()
    if bot is None:
        return

    # Accept both a single VpnKey and a list[VpnKey]
    if not isinstance(vpn_keys, list):
        vpn_keys = [vpn_keys]

    try:
        first_url: str = ""

        if len(vpn_keys) == 1:
            url = get_access_url(vpn_keys[0])  # type: ignore[arg-type]
            first_url = url
            text = (
                "✅ Ваш ключ доступа готов!\n\n"
                f"<code>{url}</code>\n\n"
                "📱 <b>Как подключиться:</b>\n"
                "Скопируйте ключ и импортируйте в приложение:\n"
                "• <b>V2RayNG</b> (Android)\n"
                "• <b>Streisand</b> или <b>FoXray</b> (iPhone)\n"
                "• <b>NekoBox</b> или <b>Hiddify</b> (Windows/Mac)\n\n"
                "Нажмите «➕ Добавить» или «Импорт» в приложении."
            )
        else:
            lines = ["✅ Ваши ключи доступа готовы! Подключайтесь к любому серверу.\n"]
            for key in vpn_keys:
                url = get_access_url(key)  # type: ignore[arg-type]
                if not first_url:
                    first_url = url
                label = getattr(key, "label", None) or "VPN"
                lines.append(f"<b>{label}:</b>\n<code>{url}</code>\n")
            lines.append(
                "📱 <b>Как подключиться:</b>\n"
                "Скопируйте любой ключ и импортируйте в приложение:\n"
                "• <b>V2RayNG</b> (Android)\n"
                "• <b>Streisand</b> или <b>FoXray</b> (iPhone)\n"
                "• <b>NekoBox</b> или <b>Hiddify</b> (Windows/Mac)\n\n"
                "Нажмите «➕ Добавить» или «Импорт» в приложении."
            )
            text = "\n".join(lines)

        await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")

        try:
            from bot.handlers.onboarding import send_onboarding_message
            await send_onboarding_message(user_id, first_url)
        except Exception as onboard_exc:
            logger.error("send_onboarding_message failed for user %d: %s", user_id, onboard_exc)
    except Exception as exc:
        logger.error("send_key_to_user failed for user %d: %s", user_id, exc)


async def notify_payment_confirmed(
    telegram_id: int,
    amount: float,
    is_renewal: bool = False,
    subscription: object = None,
) -> None:
    """Notify user that their payment was confirmed and subscription is active."""
    from app.bot_instance import get_bot

    bot = get_bot()
    if bot is None:
        return
    try:
        if is_renewal and subscription is not None:
            expires_at = getattr(subscription, "expires_at", None)
            expires_str = expires_at.strftime("%d.%m.%Y") if expires_at else "—"
            text = (
                f"✅ <b>Оплата получена!</b>\n\n"
                f"Сумма: <b>{int(amount)} ₽</b>\n"
                f"Подписка продлена до: <b>{expires_str}</b>\n\n"
                "Ваш ключ доступа остаётся прежним — пользуйтесь!"
            )
        else:
            text = (
                f"✅ <b>Оплата получена!</b>\n\n"
                f"Сумма: <b>{int(amount)} ₽</b>\n"
                "Ваша подписка активируется в течение нескольких секунд.\n"
                "Ключ доступа придёт следующим сообщением."
            )
        await bot.send_message(chat_id=telegram_id, text=text, parse_mode="HTML")
    except Exception as exc:
        logger.error("notify_payment_confirmed failed for telegram_id %d: %s", telegram_id, exc)


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
