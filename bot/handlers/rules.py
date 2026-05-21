from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database import async_session_maker
from app.models.user import User

router = Router(name="rules")

RULES_TEXT = """
📋 <b>Условия использования сервиса Private Line</b>

Сервис предоставляет защищённое интернет-соединение для личного использования.

Запрещено использование сервиса для:
• Незаконной деятельности
• Нарушения авторских прав
• Рассылки спама
• Любых действий, нарушающих законодательство РФ

Сервис предназначен для защиты вашего соединения в публичных Wi-Fi сетях и при работе из других стран.

Нажимая «Принять», вы соглашаетесь с условиями использования.
""".strip()


def rules_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Принять условия", callback_data="accept_rules_only:1"),
        ]]
    )


@router.message(Command("rules"))
@router.message(F.text == "📋 Правила сервиса")
async def show_rules(message: Message) -> None:
    await message.answer(RULES_TEXT, parse_mode="HTML", reply_markup=rules_keyboard())


@router.callback_query(F.data.startswith("accept_rules:"))
async def accept_rules_and_continue(callback: CallbackQuery, db_user: User) -> None:
    """Accept rules from start flow — show main menu after accepting."""
    async with async_session_maker() as session:
        user = await session.get(User, db_user.id)
        if user is not None and user.rules_accepted_at is None:
            user.rules_accepted_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.commit()
    from bot.keyboards.main_menu import main_menu_keyboard
    await callback.message.edit_text(  # type: ignore[union-attr]
        "✅ Условия приняты. Добро пожаловать в Private Line!\n\n"
        "Выберите действие в меню ниже.",
    )
    await callback.message.answer(  # type: ignore[union-attr]
        "Главное меню:",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("accept_rules_only:"))
async def accept_rules_only(callback: CallbackQuery, db_user: User) -> None:
    """Accept rules outside of purchase flow (from /rules command)."""
    async with async_session_maker() as session:
        user = await session.get(User, db_user.id)
        if user is not None and user.rules_accepted_at is None:
            user.rules_accepted_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.commit()
    await callback.message.edit_text(  # type: ignore[union-attr]
        "✅ Условия приняты. Добро пожаловать в Private Line!"
    )
    await callback.answer()
