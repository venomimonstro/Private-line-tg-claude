from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from app.database import async_session_maker
from app.enums import TicketStatus
from app.models.ticket import Ticket
from app.models.ticket_message import TicketMessage
from app.models.user import User
from app.services.notification import notify_admin
from bot.states import SupportStates

router = Router(name="support")

_CANCEL_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


@router.message(Command("support"))
@router.message(F.text == "🆘 Поддержка")
async def on_support(message: Message, state: FSMContext) -> None:
    await state.set_state(SupportStates.waiting_for_message)
    await message.answer(
        "🆘 <b>Поддержка</b>\n\n"
        "Опишите вашу проблему или вопрос одним сообщением.\n"
        "Мы ответим в ближайшее время.",
        reply_markup=_CANCEL_KB,
    )


@router.message(StateFilter(SupportStates.waiting_for_message), F.text == "❌ Отмена")
async def on_support_cancel(message: Message, state: FSMContext) -> None:
    from bot.keyboards.main_menu import main_menu_keyboard
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_keyboard())


@router.message(StateFilter(SupportStates.waiting_for_message))
async def on_support_message(message: Message, state: FSMContext, db_user: User) -> None:
    from bot.keyboards.main_menu import main_menu_keyboard

    text = message.text or message.caption or ""
    if not text.strip():
        await message.answer("Пожалуйста, отправьте текстовое сообщение.")
        return

    await state.clear()

    async with async_session_maker() as session:
        now = datetime.utcnow()
        ticket = Ticket(
            user_id=db_user.id,
            status=TicketStatus.open,
            created_at=now,
            updated_at=now,
        )
        session.add(ticket)
        await session.flush()

        tmsg = TicketMessage(
            ticket_id=ticket.id,
            author_type="user",
            text=text.strip(),
            created_at=now,
        )
        session.add(tmsg)
        await session.commit()
        ticket_id = ticket.id

    name = db_user.first_name or db_user.username or f"ID:{db_user.id}"
    tg_ref = f"@{db_user.username}" if db_user.username else str(db_user.telegram_id)
    await notify_admin(
        f"🆘 <b>Новый тикет #{ticket_id}</b>\n"
        f"Пользователь: {name} ({tg_ref})\n\n"
        f"{text[:500]}"
    )

    await message.answer(
        f"✅ <b>Тикет #{ticket_id} создан</b>\n\n"
        "Мы рассмотрим ваш запрос и ответим вам здесь в боте.",
        reply_markup=main_menu_keyboard(),
    )
