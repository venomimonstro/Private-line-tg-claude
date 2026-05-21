from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.models.user import User

logger = logging.getLogger(__name__)

router = Router(name="onboarding")

CONNECTION_INSTRUCTIONS: dict[str, str] = {
    "android": (
        "📱 <b>Подключение на Android</b>\n\n"
        "1️⃣ Установите приложение <b>Outline</b> из Google Play.\n"
        "2️⃣ Нажмите кнопку <b>+</b> в правом нижнем углу.\n"
        "3️⃣ Вставьте скопированный ключ доступа.\n"
        "4️⃣ Нажмите <b>«Подключиться»</b>.\n\n"
        "✅ Готово! Подключение защищено."
    ),
    "iphone": (
        "🍎 <b>Подключение на iPhone</b>\n\n"
        "1️⃣ Установите приложение <b>Outline</b> из App Store.\n"
        "2️⃣ Нажмите кнопку <b>+</b> в правом нижнем углу.\n"
        "3️⃣ Вставьте скопированный ключ доступа.\n"
        "4️⃣ Разрешите добавление VPN-конфигурации и нажмите <b>«Подключиться»</b>.\n\n"
        "✅ Готово! Подключение защищено."
    ),
    "windows": (
        "🖥 <b>Подключение на Windows</b>\n\n"
        "1️⃣ Скачайте приложение <b>Outline</b> с официального сайта getoutline.org.\n"
        "2️⃣ Запустите установщик и дождитесь завершения.\n"
        "3️⃣ Нажмите <b>+</b> и вставьте ключ доступа.\n"
        "4️⃣ Нажмите <b>«Подключиться»</b>.\n\n"
        "✅ Готово! Подключение защищено."
    ),
    "macos": (
        "🍏 <b>Подключение на macOS</b>\n\n"
        "1️⃣ Установите приложение <b>Outline</b> из Mac App Store.\n"
        "2️⃣ Откройте приложение и нажмите <b>+</b>.\n"
        "3️⃣ Вставьте скопированный ключ доступа.\n"
        "4️⃣ Разрешите добавление конфигурации и нажмите <b>«Подключиться»</b>.\n\n"
        "✅ Готово! Подключение защищено."
    ),
}


def _platform_keyboard(access_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🤖 Android", callback_data="onboard_platform:android"),
                InlineKeyboardButton(text="🍎 iPhone", callback_data="onboard_platform:iphone"),
            ],
            [
                InlineKeyboardButton(text="🖥 Windows", callback_data="onboard_platform:windows"),
                InlineKeyboardButton(text="🍏 macOS", callback_data="onboard_platform:macos"),
            ],
            [
                InlineKeyboardButton(
                    text="📱 Открыть Outline",
                    url=f"https://s3.amazonaws.com/outline-vpn/index.html#{access_url}",
                )
            ],
            [
                InlineKeyboardButton(text="✅ Проверить подключение", callback_data="connection_check"),
            ],
        ]
    )


async def send_onboarding_message(
    user_id: int,
    access_url: str,
    platform_hint: str | None = None,
) -> None:
    """Send step-by-step onboarding after key delivery."""
    from app.bot_instance import get_bot

    bot = get_bot()
    if bot is None:
        return

    text = (
        "🎉 <b>Ваш VPN-ключ готов!</b>\n\n"
        "Скопируйте ключ ниже и добавьте его в приложение Outline:\n\n"
        f"<code>{access_url}</code>\n\n"
        "Выберите вашу платформу для пошаговой инструкции:"
    )

    try:
        await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=_platform_keyboard(access_url),
        )
    except Exception as exc:
        logger.error("send_onboarding_message failed for user %d: %s", user_id, exc)


@router.callback_query(F.data.startswith("onboard_platform:"))
async def on_platform_select(callback: CallbackQuery) -> None:
    platform = callback.data.split(":", 1)[1]  # type: ignore[union-attr]
    steps_text = CONNECTION_INSTRUCTIONS.get(platform)

    if steps_text is None:
        await callback.answer("Неизвестная платформа.", show_alert=True)
        return

    try:
        await callback.message.answer(steps_text, parse_mode="HTML")  # type: ignore[union-attr]
    except Exception as exc:
        logger.error("on_platform_select send failed: %s", exc)

    await callback.answer()


@router.callback_query(F.data == "connection_check")
async def on_connection_check(callback: CallbackQuery, db_user: User) -> None:
    from datetime import datetime

    from sqlalchemy import select

    from app.database import async_session_maker
    from app.enums import SubscriptionStatus, VpnKeyStatus
    from app.models.subscription import Subscription
    from app.models.vpn_key import VpnKey

    async with async_session_maker() as session:
        result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == db_user.id,
                Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
            ).order_by(Subscription.created_at.desc())
        )
        sub = result.scalars().first()

        if sub is None:
            await callback.answer("❌ Активная подписка не найдена.", show_alert=True)
            return

        key_result = await session.execute(
            select(VpnKey).where(
                VpnKey.subscription_id == sub.id,
                VpnKey.status == VpnKeyStatus.active,
            ).limit(1)
        )
        vpn_key = key_result.scalar_one_or_none()

    if vpn_key is None:
        await callback.answer(
            "❌ Активный ключ не найден. Обратитесь в поддержку.",
            show_alert=True,
        )
        return

    days_since = (datetime.utcnow() - vpn_key.created_at).days
    await callback.answer(
        f"✅ Ваша подписка активна. Ключ выдан {days_since} дн. назад.",
        show_alert=True,
    )
