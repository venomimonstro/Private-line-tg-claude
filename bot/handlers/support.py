from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

router = Router(name="support")

_SUPPORT_USERNAME = "Privatexlinehelp_bot"

_TEXT = (
    "🆘 <b>Поддержка</b>\n\n"
    "Если у вас возникли вопросы или проблемы — напишите нам напрямую.\n\n"
    "Мы отвечаем быстро и помогаем с любыми вопросами по подключению."
)

_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="💬 Написать в поддержку", url=f"https://t.me/{_SUPPORT_USERNAME}")],
])


@router.message(Command("support"))
@router.message(F.text == "🆘 Поддержка")
async def on_support(message: Message) -> None:
    await message.answer(_TEXT, parse_mode="HTML", reply_markup=_KB)
