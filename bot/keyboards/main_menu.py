from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💳 Купить доступ"), KeyboardButton(text="🔑 Мой доступ")],
            [KeyboardButton(text="🔄 Продлить"), KeyboardButton(text="📱 Как подключиться")],
            [KeyboardButton(text="📊 Проверить лимит"), KeyboardButton(text="❓ Помощь")],
            [KeyboardButton(text="👥 Пригласить друга"), KeyboardButton(text="📋 Правила сервиса")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def tariff_selection_keyboard(tariffs: list) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"{t.name} — {int(t.price_monthly)} ₽/мес", callback_data=f"tariff:{t.slug}")]
        for t in tariffs
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def accept_rules_keyboard(tariff_slug: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я принимаю правила сервиса", callback_data=f"accept_rules:{tariff_slug}")],
            [InlineKeyboardButton(text="← Назад", callback_data="back_to_plans")],
        ]
    )


def my_access_keyboard(
    active_key_ids: list[int] | None = None,
) -> InlineKeyboardMarkup:
    """Build the 'My Access' inline keyboard.

    If active_key_ids is provided and has more than one key, individual key
    buttons are shown (🔑 Ключ 1, 🔑 Ключ 2 …). Otherwise the generic
    "get_key" button is shown.
    """
    rows: list[list[InlineKeyboardButton]] = []

    if active_key_ids and len(active_key_ids) > 1:
        for idx, key_id in enumerate(active_key_ids, start=1):
            rows.append(
                [InlineKeyboardButton(text=f"🔑 Ключ {idx}", callback_data=f"get_key:{key_id}")]
            )
    else:
        rows.append([InlineKeyboardButton(text="🔑 Получить ключ", callback_data="get_key")])

    rows.append([InlineKeyboardButton(text="🌍 Сменить сервер", callback_data="geo_switch")])
    rows.append([InlineKeyboardButton(text="🔄 Продлить", callback_data="renew")])
    rows.append([InlineKeyboardButton(text="❓ Помощь", callback_data="help")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def connection_platforms_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Android", callback_data="instructions:android")],
            [InlineKeyboardButton(text="🍎 iPhone", callback_data="instructions:iphone")],
            [InlineKeyboardButton(text="🖥 Windows", callback_data="instructions:windows")],
            [InlineKeyboardButton(text="🍏 macOS", callback_data="instructions:macos")],
        ]
    )
