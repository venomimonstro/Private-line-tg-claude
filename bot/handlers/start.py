from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

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


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(_WELCOME_TEXT, parse_mode="HTML", reply_markup=main_menu_keyboard())
