from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.models.user import User
from app.services.vpn_key import get_access_url
from bot.handlers.access import _get_active_subscription

router = Router(name="mykey")


def _keys_keyboard(keys: list) -> InlineKeyboardMarkup:
    rows = []
    for idx, key in enumerate(keys, start=1):
        label = key.label or f"Ключ {idx}"
        rows.append(
            [InlineKeyboardButton(text=f"🔑 {label}", callback_data=f"get_key:{key.id}")]
        )
        rows.append(
            [InlineKeyboardButton(text=f"📨 Переслать {label}", callback_data=f"resend_key:{key.id}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("mykey"))
@router.message(F.text == "🔑 Мой ключ")
async def cmd_mykey(message: Message, db_user: User) -> None:
    _sub, active_keys, _tariff = await _get_active_subscription(db_user.id)

    if not active_keys:
        await message.answer(
            "У вас нет активных ключей.\n\nНажмите «💳 Купить доступ», чтобы оформить подписку."
        )
        return

    if len(active_keys) == 1:
        key = active_keys[0]
        try:
            url = get_access_url(key)
        except Exception:
            await message.answer("Ошибка получения ключа. Обратитесь в поддержку.")
            return
        label_part = f" ({key.label})" if key.label else ""
        await message.answer(
            f"🔑 <b>Ваш ключ доступа{label_part}:</b>\n\n"
            f"<code>{url}</code>\n\n"
            f"Скопируйте и добавьте в приложение VLESS (V2RayNG, Hiddify, FoXray).",
            parse_mode="HTML",
        )
    else:
        lines = ["🔑 <b>Ваши ключи доступа:</b>\n"]
        for key in active_keys:
            try:
                url = get_access_url(key)
            except Exception:
                continue
            label = key.label or "VPN"
            lines.append(f"<b>{label}:</b>\n<code>{url}</code>")
        lines.append(
            "\n📱 Скопируйте нужный ключ и добавьте в приложение:\n"
            "• <b>V2RayNG</b> (Android)\n• <b>Streisand / FoXray</b> (iPhone)\n"
            "• <b>Hiddify</b> (Windows/Mac)"
        )
        await message.answer("\n\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data.startswith("resend_key:"))
async def on_resend_key(callback: CallbackQuery, db_user: User) -> None:
    try:
        key_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный запрос.", show_alert=True)
        return

    _sub, active_keys, _tariff = await _get_active_subscription(db_user.id)
    key = next((k for k in active_keys if k.id == key_id), None)

    if key is None:
        await callback.answer("Ключ не найден или не принадлежит вам.", show_alert=True)
        return

    try:
        url = get_access_url(key)
    except Exception:
        await callback.answer("Ошибка получения ключа. Обратитесь в поддержку.", show_alert=True)
        return

    label_part = f" ({key.label})" if key.label else ""
    await callback.message.answer(  # type: ignore[union-attr]
        f"📨 <b>Ключ доступа{label_part} повторно:</b>\n\n"
        f"<code>{url}</code>\n\n"
        f"Скопируйте ключ и добавьте его в приложение Outline.",
        parse_mode="HTML",
    )
    await callback.answer("Ключ отправлен повторно.")
