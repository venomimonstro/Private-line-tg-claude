from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def send_key_to_user(user_id: int, vpn_keys: object) -> None:
    """Send all access keys to user after activation, then send onboarding."""
    from app.bot_instance import get_bot
    from app.services.vpn_key import get_client_access_url

    bot = get_bot()
    temporary_bot = False
    if bot is None:
        # Admin/worker processes do not share the polling process's bot singleton.
        # Use a short-lived Telegram client rather than silently skipping delivery.
        from aiogram import Bot
        from app.config import settings
        bot = Bot(token=settings.BOT_TOKEN)
        temporary_bot = True

    # Accept both a single VpnKey and a list[VpnKey]
    if not isinstance(vpn_keys, list):
        vpn_keys = [vpn_keys]

    try:
        first_url: str = ""

        _app_hint = (
            "📱 <b>Приложения для подключения:</b>\n"
            "• <b>Android:</b> Hiddify, V2RayNG\n"
            "• <b>iPhone:</b> Streisand, FoXray, V2Box\n"
            "• <b>Windows/macOS:</b> Hiddify\n"
            "• <b>Linux:</b> Hiddify, v2rayA\n\n"
            "Скопируйте ключ → откройте приложение → нажмите «+» → «Импорт из буфера».\n"
            "Подробная инструкция: кнопка «📱 Как подключиться» в меню."
        )

        from html import escape

        if len(vpn_keys) == 1:
            url = get_client_access_url(vpn_keys[0])  # type: ignore[arg-type]
            first_url = url
            text = (
                "✅ <b>Ваш VPN-ключ готов!</b>\n\n"
                f"<code>{escape(url)}</code>\n\n"
                + _app_hint
            )
        else:
            lines = ["✅ <b>Ваши VPN-ключи готовы!</b>\n"]
            for key in vpn_keys:
                url = get_client_access_url(key)  # type: ignore[arg-type]
                if not first_url:
                    first_url = url
                label = getattr(key, "label", None) or "VPN"
                lines.append(f"<b>{escape(str(label))}:</b>\n<code>{escape(url)}</code>\n")
            lines.append(_app_hint)
            text = "\n".join(lines)

        await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")

        try:
            from bot.handlers.onboarding import send_onboarding_message
            await send_onboarding_message(user_id, first_url)
        except Exception as onboard_exc:
            logger.error("send_onboarding_message failed for user %d: %s", user_id, onboard_exc)
    except Exception as exc:
        logger.error("send_key_to_user failed for Telegram chat %d: %s", user_id, exc)
        raise
    finally:
        if temporary_bot:
            await bot.session.close()


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


async def notify_referral_milestone(
    telegram_id: int,
    friend_count: int,
    bonus_days: int,
) -> None:
    """Notify referrer that they hit a milestone bonus."""
    from app.bot_instance import get_bot

    bot = get_bot()
    if bot is None:
        return
    month_map = {7: "7 дней", 30: "1 месяц", 90: "3 месяца"}
    bonus_str = month_map.get(bonus_days, f"{bonus_days} дней")
    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"🎉 <b>Milestone!</b> Вы пригласили <b>{friend_count}</b> "
                f"{'друга' if friend_count == 1 else 'друзей'}!\n\n"
                f"🎁 Ваша подписка продлена на <b>{bonus_str}</b> бесплатно."
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("notify_referral_milestone failed for %d: %s", telegram_id, exc)


async def notify_activation_failed(telegram_id: int) -> None:
    """Notify user that key activation failed — they should contact support."""
    from app.bot_instance import get_bot

    bot = get_bot()
    if bot is None:
        return
    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                "⚠️ <b>Не удалось выдать VPN-ключ</b>\n\n"
                "Мы уже работаем над этим — ключ будет выдан автоматически "
                "в течение нескольких минут.\n\n"
                "Если ключ не придёт в течение 10 минут — напишите в поддержку."
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("notify_activation_failed failed for %d: %s", telegram_id, exc)


async def notify_gift_sent(sender_telegram_id: int, recipient_telegram_id: int, tariff_name: str) -> None:
    """Notify gift sender that the gift was delivered."""
    from app.bot_instance import get_bot

    bot = get_bot()
    if bot is None:
        return
    try:
        await bot.send_message(
            chat_id=sender_telegram_id,
            text=(
                f"🎁 Ваш подарок отправлен!\n\n"
                f"Тариф <b>{tariff_name}</b> активирован для пользователя "
                f"<code>{recipient_telegram_id}</code>.\n\n"
                "Спасибо за то, что делитесь Private X Line с друзьями!"
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("notify_gift_sent failed for sender %d: %s", sender_telegram_id, exc)
