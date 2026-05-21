from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app.config import settings
from app.models.user import User

router = Router(name="support")
logger = logging.getLogger(__name__)


class SupportStates(StatesGroup):
    waiting_message = State()


_PROMPT = (
    "💬 <b>Написать в поддержку</b>\n\n"
    "Опишите ваш вопрос или проблему. Мы ответим как можно скорее.\n\n"
    "Для отмены отправьте /cancel."
)


@router.message(Command("support"))
@router.message(F.text == "💬 Поддержка")
async def on_support_start(message: Message, state: FSMContext) -> None:
    await state.set_state(SupportStates.waiting_message)
    await message.answer(_PROMPT, parse_mode="HTML")


@router.message(Command("cancel"), SupportStates.waiting_message)
async def on_support_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Обращение отменено.")


@router.message(SupportStates.waiting_message)
async def on_support_message(message: Message, state: FSMContext, db_user: User) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пожалуйста, отправьте текстовое сообщение.")
        return

    await state.clear()

    username_part = f"@{db_user.username}" if db_user.username else f"id={db_user.telegram_id}"
    name_part = db_user.first_name or ""
    admin_text = (
        f"📩 <b>Обращение в поддержку</b>\n\n"
        f"От: {name_part} {username_part} (DB#{db_user.id})\n\n"
        f"{text}"
    )

    forwarded = False
    if settings.ADMIN_CHAT_ID:
        try:
            from app.bot_instance import get_bot
            bot = get_bot()
            if bot is not None:
                await bot.send_message(
                    chat_id=settings.ADMIN_CHAT_ID,
                    text=admin_text,
                    parse_mode="HTML",
                )
                forwarded = True
        except Exception as exc:
            logger.error("Failed to forward support message to admin: %s", exc)

    if forwarded:
        await message.answer(
            "✅ Ваше сообщение отправлено в поддержку.\n"
            "Мы ответим вам в ближайшее время."
        )
    else:
        await message.answer(
            "⚠️ Не удалось отправить сообщение. Попробуйте позже или напишите напрямую администратору."
        )
