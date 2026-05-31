from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database import async_session_maker
from app.models.tariff import Tariff
from app.models.user import User
from bot.keyboards.main_menu import main_menu_keyboard
from sqlalchemy import select

router = Router(name="start")

_WELCOME_BASE = (
    "👋 Привет! Это <b>Private Line</b> — сервис защищённого интернет-доступа "
    "для телефона и ноутбука.\n\n"
    "Подходит для работы, поездок и подключения через публичные Wi-Fi.\n\n"
)

_WELCOME_FOOTER = (
    "\nПодключение занимает около 2 минут.\n\n"
    "Выберите тариф, оплатите доступ и получите персональный ключ "
    "с инструкцией прямо в этом боте."
)

_WELCOME_WITH_RULES_TEXT = (
    "👋 Привет! Это <b>Private Line</b> — сервис защищённого интернет-доступа "
    "для телефона и ноутбука.\n\n"
    "Перед использованием сервиса, пожалуйста, ознакомьтесь с условиями использования."
)


async def _build_welcome_text() -> str:
    async with async_session_maker() as session:
        result = await session.execute(
            select(Tariff)
            .where(
                Tariff.is_active == True,
                Tariff.is_visible == True,
                Tariff.trial_days.is_(None),
            )
            .order_by(Tariff.price_monthly.asc(), Tariff.sort_order.asc())
        )
        tariffs = result.scalars().all()

    if not tariffs:
        return _WELCOME_BASE + _WELCOME_FOOTER

    t = tariffs[0]  # cheapest regular tariff
    if t.price_first_month is not None:
        price_line = (
            f"📌 Первый месяц — <b>{int(t.price_first_month)} ₽</b>, "
            f"далее — <b>{int(t.price_monthly)} ₽/мес</b>."
        )
    else:
        price_line = f"📌 <b>{int(t.price_monthly)} ₽/мес</b>."

    return _WELCOME_BASE + price_line + _WELCOME_FOOTER


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
        welcome_text = await _build_welcome_text()
        await message.answer(welcome_text, parse_mode="HTML", reply_markup=main_menu_keyboard())
