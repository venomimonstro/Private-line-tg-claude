from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.models.user import User
from bot.keyboards.main_menu import main_menu_keyboard

router = Router(name="start")

_WELCOME_TEXT = (
    "👋 Привет! Это <b>Private Line</b> — сервис защищённого интернет-доступа "
    "для телефона и ноутбука.\n\n"
    "Подходит для работы, поездок и подключения через публичные Wi-Fi.\n\n"
    "📌 Первый месяц — <b>99 ₽</b>, далее — <b>149 ₽/мес</b>.\n"
    "Подключение занимает около 2 минут.\n\n"
    "Выберите тариф, оплатите доступ и получите персональный ключ "
    "с инструкцией прямо в этом боте."
)

_WELCOME_WITH_RULES_TEXT = (
    "👋 Привет! Это <b>Private Line</b> — сервис защищённого интернет-доступа "
    "для телефона и ноутбука.\n\n"
    "Перед использованием сервиса, пожалуйста, ознакомьтесь с условиями использования."
)


def _accept_rules_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Принять условия",
                callback_data="accept_rules:1",
            )
        ]]
    )


@router.message(Command("start"))
async def cmd_start(message: Message, db_user: User) -> None:
    if db_user.rules_accepted_at is None:
        from bot.handlers.rules import RULES_TEXT
        await message.answer(
            _WELCOME_WITH_RULES_TEXT + "\n\n" + RULES_TEXT,
            parse_mode="HTML",
            reply_markup=_accept_rules_keyboard(),
        )
    else:
        await message.answer(_WELCOME_TEXT, parse_mode="HTML", reply_markup=main_menu_keyboard())
