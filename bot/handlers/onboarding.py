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
        "Для сервера Spain-1 используйте <code>vless://</code> (Reality/Vision). Приложение Outline этот формат не поддерживает.\n\n"
        "<b>Рекомендуемые приложения:</b>\n\n"
        "🔹 <b>Hiddify</b> (проще всего)\n"
        "   • Google Play → «Hiddify»\n"
        "   • Нажмите <b>+</b> → «Добавить из буфера обмена»\n"
        "   • Нажмите ▶️ для подключения\n\n"
        "🔹 <b>V2RayNG</b>\n"
        "   • Google Play → «v2rayNG»\n"
        "   • Нажмите <b>+</b> → «Импорт конфигурации из буфера»\n"
        "   • Нажмите ▶️ для подключения\n\n"
        "🔹 <b>NekoBox</b> (для продвинутых)\n"
        "   • Скачайте с GitHub: github.com/MatsuriDayo/NekoBoxForAndroid\n"
        "   • Нажмите <b>+</b> → «Добавить из буфера»\n\n"
        "💡 <i>После вставки ключа появится конфигурация — просто нажмите подключить.</i>\n\n"
        "✅ Готово! Весь трафик защищён."
    ),
    "iphone": (
        "🍎 <b>Подключение на iPhone / iPad</b>\n\n"
        "Для сервера Spain-1 используйте <code>vless://</code> (Reality/Vision). В Outline он не работает.\n\n"
        "<b>Рекомендуемые приложения:</b>\n\n"
        "🔹 <b>Streisand</b> (бесплатно, рекомендуем)\n"
        "   • App Store → «Streisand»\n"
        "   • Нажмите <b>+</b> → «Импорт из буфера обмена»\n"
        "   • Разрешите VPN при запросе → нажмите подключить\n\n"
        "🔹 <b>FoXray</b> (бесплатно)\n"
        "   • App Store → «FoXray»\n"
        "   • Нажмите <b>+</b> → «Импорт из буфера»\n"
        "   • Разрешите VPN → нажмите подключить\n\n"
        "🔹 <b>V2Box</b> (бесплатно)\n"
        "   • App Store → «V2Box - V2ray VPN Client»\n"
        "   • Нажмите <b>+</b> → «Вставить из буфера»\n\n"
        "🔹 <b>Shadowrocket</b> (платно, $2.99)\n"
        "   • App Store → «Shadowrocket»\n"
        "   • Нажмите <b>+</b> → «URL» → вставьте ключ\n\n"
        "💡 <i>Если появится запрос «Добавить конфигурацию VPN» — нажмите «Разрешить».</i>\n\n"
        "✅ Готово! Весь трафик защищён."
    ),
    "windows": (
        "🖥 <b>Подключение на Windows</b>\n\n"
        "Для сервера Spain-1 используйте <code>vless://</code> (Reality/Vision). В Outline он не работает.\n\n"
        "<b>Рекомендуемые приложения:</b>\n\n"
        "🔹 <b>Hiddify</b> (проще всего, рекомендуем)\n"
        "   • Скачайте с hiddify.com → раздел «Windows»\n"
        "   • Нажмите <b>+</b> → «Добавить из буфера обмена»\n"
        "   • Нажмите подключиться\n\n"
        "🔹 <b>v2rayN</b>\n"
        "   • GitHub: github.com/2dust/v2rayN → скачайте последний релиз\n"
        "   • Нажмите <b>+</b> → «Добавить сервер из буфера»\n"
        "   • ПКМ по серверу → «Установить как активный» → подключить\n\n"
        "🔹 <b>NekoRay / NekoBox</b>\n"
        "   • GitHub: github.com/MatsuriDayo/nekoray\n"
        "   • «Программа» → «Добавить профиль из буфера»\n\n"
        "🔹 <b>Clash Verge Rev</b>\n"
        "   • GitHub: github.com/clash-verge-rev/clash-verge-rev\n"
        "   • Профили → «Импорт» → вставьте ключ\n\n"
        "💡 <i>Если Windows Defender предупреждает — нажмите «Дополнительно» → «Всё равно выполнить».</i>\n\n"
        "✅ Готово! Весь трафик защищён."
    ),
    "macos": (
        "🍏 <b>Подключение на macOS</b>\n\n"
        "Для сервера Spain-1 используйте <code>vless://</code> (Reality/Vision). В Outline он не работает.\n\n"
        "<b>Рекомендуемые приложения:</b>\n\n"
        "🔹 <b>Hiddify</b> (проще всего, рекомендуем)\n"
        "   • Скачайте с hiddify.com → раздел «macOS»\n"
        "   • Нажмите <b>+</b> → «Добавить из буфера обмена»\n"
        "   • Подтвердите VPN → нажмите подключить\n\n"
        "🔹 <b>FoXray</b> (бесплатно, Mac App Store)\n"
        "   • App Store → «FoXray»\n"
        "   • Нажмите <b>+</b> → «Импорт из буфера»\n"
        "   • Разрешите VPN-конфигурацию\n\n"
        "🔹 <b>Clash Verge Rev</b>\n"
        "   • GitHub: github.com/clash-verge-rev/clash-verge-rev\n"
        "   • Профили → «Импорт» → вставьте ключ\n\n"
        "🔹 <b>V2RayXS / V2RayU</b>\n"
        "   • Mac App Store → «V2RayXS»\n"
        "   • Настройки → «Импорт из буфера»\n\n"
        "💡 <i>При первом запуске macOS может попросить разрешение в «Системных настройках → Конфиденциальность».</i>\n\n"
        "✅ Готово! Весь трафик защищён."
    ),
    "linux": (
        "🐧 <b>Подключение на Linux</b>\n\n"
        "Для сервера Spain-1 используйте <code>vless://</code> (Reality/Vision).\n\n"
        "<b>Рекомендуемые приложения:</b>\n\n"
        "🔹 <b>Hiddify</b> (GUI, проще всего)\n"
        "   • Скачайте AppImage с hiddify.com → «Linux»\n"
        "   • <code>chmod +x hiddify.AppImage && ./hiddify.AppImage</code>\n"
        "   • Нажмите <b>+</b> → «Добавить из буфера обмена»\n\n"
        "🔹 <b>v2rayA</b> (веб-интерфейс)\n"
        "   • Установка: v2raya.org/docs/install\n"
        "   • <code>sudo systemctl start v2raya</code>\n"
        "   • Откройте браузер: http://localhost:2017\n"
        "   • «Импорт» → вставьте ключ\n\n"
        "🔹 <b>NekoRay</b> (GUI)\n"
        "   • GitHub: github.com/MatsuriDayo/nekoray → Linux release\n"
        "   • «Программа» → «Добавить профиль из буфера»\n\n"
        "🔹 <b>sing-box CLI</b> (терминал)\n"
        "   • Установка: sing-box.sagernet.org\n"
        "   • Конфигурация через JSON\n\n"
        "💡 <i>Для работы системного прокси может потребоваться включить «Системный прокси» в настройках приложения.</i>\n\n"
        "✅ Готово! Весь трафик защищён."
    ),
    "router": (
        "📡 <b>Подключение на роутере</b>\n\n"
        "Подключив VPN на роутере, все устройства в сети защищены автоматически.\n\n"
        "<b>Поддерживаемые прошивки:</b>\n\n"
        "🔹 <b>OpenWrt</b>\n"
        "   • Установите пакет <code>sing-box</code> или <code>hiddify</code>\n"
        "   • Настройте через LuCI или UCI\n"
        "   • Документация: sing-box.sagernet.org\n\n"
        "🔹 <b>Keenetic (NDMS)</b>\n"
        "   • Компонент «Политики соединений»\n"
        "   • Установите Entware → sing-box через opkg\n\n"
        "🔹 <b>Mikrotik</b>\n"
        "   • Используйте контейнер Docker с sing-box\n\n"
        "🔹 <b>Asus (AsusWRT / Merlin)</b>\n"
        "   • Entware → sing-box или shadowsocks-libev\n\n"
        "💡 <i>Для настройки роутера рекомендуем обратиться в нашу поддержку — поможем с конфигурацией.</i>\n\n"
        "⚠️ <i>Требуются базовые навыки работы с роутерами.</i>"
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
                InlineKeyboardButton(text="🐧 Linux", callback_data="onboard_platform:linux"),
                InlineKeyboardButton(text="📡 Роутер", callback_data="onboard_platform:router"),
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
        "Скопируйте ключ выше и добавьте его в приложение.\n\n"
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
