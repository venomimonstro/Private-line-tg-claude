from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.services.vpn_key import get_access_url
from bot.handlers.access import _get_active_subscription
from app.models.user import User

logger = logging.getLogger(__name__)

router = Router(name="geo_switch")


@router.callback_query(F.data == "geo_switch")
async def on_geo_switch_start(callback: CallbackQuery, db_user: User) -> None:
    sub, active_keys, _ = await _get_active_subscription(db_user.id)

    if not active_keys:
        await callback.answer("У вас нет активных ключей.", show_alert=True)
        return

    if len(active_keys) == 1:
        key = active_keys[0]
        try:
            url = get_access_url(key)
        except Exception:
            await callback.answer("Ошибка получения ключа.", show_alert=True)
            return
        label = key.label or "VPN"
        await callback.message.answer(  # type: ignore[union-attr]
            f"🔑 <b>Ваш ключ ({label}):</b>\n\n<code>{url}</code>\n\n"
            f"Скопируйте и добавьте в приложение.",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    rows = []
    for key in active_keys:
        label = key.label or "VPN"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"geo_view:{key.id}")])

    await callback.message.answer(  # type: ignore[union-attr]
        "🌍 <b>Выберите страну подключения:</b>\n\nКаждый ключ работает на отдельном сервере.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("geo_view:"))
async def on_geo_view_key(callback: CallbackQuery, db_user: User) -> None:
    try:
        key_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный запрос.", show_alert=True)
        return

    _, active_keys, _ = await _get_active_subscription(db_user.id)
    key = next((k for k in active_keys if k.id == key_id), None)

    if key is None:
        await callback.answer("Ключ не найден.", show_alert=True)
        return

    try:
        url = get_access_url(key)
    except Exception:
        await callback.answer("Ошибка получения ключа.", show_alert=True)
        return

    label = key.label or "VPN"
    await callback.message.answer(  # type: ignore[union-attr]
        f"🔑 <b>Ключ для {label}:</b>\n\n<code>{url}</code>\n\n"
        f"Скопируйте и импортируйте в приложение:\n"
        f"• <b>V2RayNG</b> (Android)\n• <b>Streisand / FoXray</b> (iPhone)\n"
        f"• <b>Hiddify</b> (Windows/Mac)",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "back_to_access")
async def on_back_to_access(callback: CallbackQuery) -> None:
    await callback.message.delete()  # type: ignore[union-attr]
    await callback.answer()
