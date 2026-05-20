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

_RULES_TEXT = (
    "📋 <b>Правила сервиса Private Line</b>\n\n"
    "1. Сервис предназначен исключительно для защищённого интернет-доступа.\n"
    "2. Запрещено использование для незаконных действий, спама, фишинга, DDoS, торрентов.\n"
    "3. Запрещена передача ключа третьим лицам.\n"
    "4. При нарушении правил доступ блокируется без возврата средств.\n"
    "5. Персональные данные хранятся только в части telegram_id для работы сервиса.\n"
    "6. Подписка предоставляет защищённый доступ, не гарантирует анонимность.\n\n"
    "Полные условия доступны у администратора."
)


@router.message(F.text == "❓ Помощь")
@router.message(Command("help"))
async def show_help(message: Message) -> None:
    await message.answer(_HELP_TEXT, parse_mode="HTML")


@router.message(F.text == "📋 Правила сервиса")
@router.message(Command("rules"))
async def show_rules(message: Message) -> None:
    await message.answer(_RULES_TEXT, parse_mode="HTML")


@router.callback_query(F.data == "help")
async def on_help_callback(callback) -> None:
    await callback.message.answer(_HELP_TEXT, parse_mode="HTML")
    await callback.answer()
