from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from app.database import async_session_maker
from app.enums import TicketStatus
from app.models.ticket import Ticket
from app.models.ticket_message import TicketMessage
from app.models.user import User
from app.services.notification import notify_admin
from bot.states import SupportStates

router = Router(name="tickets")

_CANCEL_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


@router.message(F.text == "🎫 Тикеты")
async def on_tickets(message: Message, db_user: User) -> None:
    async with async_session_maker() as session:
        from sqlalchemy import select
        result = await session.execute(
            select(Ticket)
            .where(Ticket.user_id == db_user.id)
            .order_by(Ticket.updated_at.desc())
            .limit(5)
        )
        tickets = result.scalars().all()

    if not tickets:
        text = (
            "🎫 <b>Мои тикеты</b>\n\n"
            "У вас пока нет обращений в поддержку.\n\n"
            "Нажмите кнопку ниже чтобы создать новый тикет."
        )
    else:
        lines = ["🎫 <b>Мои тикеты</b>\n"]
        status_icons = {
            TicketStatus.open: "🟡",
            TicketStatus.answered: "🟢",
            TicketStatus.closed: "⚫",
        }
        for t in tickets:
            icon = status_icons.get(t.status, "•")
            date = t.created_at.strftime("%d.%m.%Y")
            lines.append(f"{icon} <b>#{t.id}</b> — {date}")
        lines.append("\n<i>🟡 ожидает · 🟢 получен ответ · ⚫ закрыт</i>")
        text = "\n".join(lines)

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Создать новый тикет", callback_data="ticket_new")],
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "ticket_new")
async def on_ticket_new(callback, state: FSMContext) -> None:
    await state.set_state(SupportStates.waiting_for_message)
    await callback.message.answer(
        "✏️ Опишите вашу проблему или вопрос одним сообщением.\n"
        "Мы ответим здесь в боте.",
        reply_markup=_CANCEL_KB,
    )
    await callback.answer()


@router.message(StateFilter(SupportStates.waiting_for_message), F.text == "❌ Отмена")
async def on_ticket_cancel(message: Message, state: FSMContext) -> None:
    from bot.keyboards.main_menu import main_menu_keyboard
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_keyboard())


@router.message(StateFilter(SupportStates.waiting_for_message))
async def on_ticket_message(message: Message, state: FSMContext, db_user: User) -> None:
    from bot.keyboards.main_menu import main_menu_keyboard

    text = (message.text or message.caption or "").strip()
    if not text:
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
            text=text,
            created_at=now,
        )
        session.add(tmsg)
        await session.commit()
        ticket_id = ticket.id

    name = db_user.first_name or db_user.username or f"ID:{db_user.id}"
    tg_ref = f"@{db_user.username}" if db_user.username else str(db_user.telegram_id)
    await notify_admin(
        f"🎫 <b>Новый тикет #{ticket_id}</b>\n"
        f"Пользователь: {name} ({tg_ref})\n\n"
        f"{text[:500]}"
    )

    await message.answer(
        f"✅ <b>Тикет #{ticket_id} создан</b>\n\n"
        "Мы рассмотрим ваш запрос и ответим вам здесь.",
        reply_markup=main_menu_keyboard(),
    )
