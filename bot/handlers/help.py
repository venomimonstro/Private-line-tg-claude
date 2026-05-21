from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="help")

_HELP_TEXT = (
    "❓ <b>Частые вопросы</b>\n\n"
    "<b>Как подключиться?</b>\n"
    "Нажмите «📱 Как подключиться» и выберите вашу платформу.\n\n"
    "<b>Как получить ключ?</b>\n"
    "Перейдите в «🔑 Мой доступ» и нажмите «Получить ключ».\n\n"
    "<b>Что делать если ключ не работает?</b>\n"
    "Обратитесь в поддержку, мы поможем.\n\n"
    "<b>Как продлить подписку?</b>\n"
    "Нажмите «🔄 Продлить», следуйте инструкции.\n\n"
    "<b>Куда обратиться за помощью?</b>\n"
    "Напишите администратору — мы ответим в ближайшее время."
)

@router.message(F.text == "❓ Помощь")
@router.message(Command("help"))
async def show_help(message: Message) -> None:
    await message.answer(_HELP_TEXT, parse_mode="HTML")


@router.callback_query(F.data == "help")
async def on_help_callback(callback) -> None:
    await callback.message.answer(_HELP_TEXT, parse_mode="HTML")
    await callback.answer()
