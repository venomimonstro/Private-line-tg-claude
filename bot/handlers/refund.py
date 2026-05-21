from __future__ import annotations
import logging
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from app.database import async_session_maker
from app.enums import PaymentStatus
from app.models.payment import Payment
from app.models.user import User
from app.services.refund import check_refund_eligibility, create_refund_request

router = Router(name="refund")
logger = logging.getLogger(__name__)


class RefundStates(StatesGroup):
    waiting_reason = State()
    confirming = State()


@router.callback_query(F.data.startswith("refund_request:"))
async def on_refund_start(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    payment_id = int(callback.data.split(":")[1])
    async with async_session_maker() as session:
        eligible, msg = await check_refund_eligibility(payment_id, db_user.id, session)

    if not eligible:
        await callback.answer(f"Возврат недоступен: {msg}", show_alert=True)
        return

    await state.update_data(payment_id=payment_id)
    await state.set_state(RefundStates.waiting_reason)
    await callback.message.answer(  # type: ignore
        "✍️ Укажите причину возврата (например: не подошло качество связи, "
        "изменились планы, техническая проблема):",
    )
    await callback.answer()


@router.message(RefundStates.waiting_reason)
async def on_refund_reason(message: Message, state: FSMContext) -> None:
    reason = (message.text or "").strip()
    if len(reason) < 10:
        await message.answer("Пожалуйста, укажите причину подробнее (минимум 10 символов).")
        return

    await state.update_data(reason=reason)
    await state.set_state(RefundStates.confirming)

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="refund_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="refund_cancel"),
    ]])
    await message.answer(
        f"Заявка на возврат:\n<b>Причина:</b> {reason}\n\nПодтверждаете?",
        parse_mode="HTML",
        reply_markup=markup,
    )


@router.callback_query(F.data == "refund_confirm", RefundStates.confirming)
async def on_refund_confirm(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    data = await state.get_data()
    payment_id = data["payment_id"]
    reason = data["reason"]
    await state.clear()

    async with async_session_maker() as session:
        req = await create_refund_request(payment_id, db_user.id, reason, session)

    if req is None:
        await callback.message.edit_text(  # type: ignore
            "❌ Не удалось создать заявку. Возможно, истёк срок или условия не выполнены."
        )
    else:
        await callback.message.edit_text(  # type: ignore
            f"✅ Заявка на возврат #{req.id} создана.\n"
            "Администратор рассмотрит её в течение 1–3 рабочих дней."
        )
    await callback.answer()


@router.callback_query(F.data == "refund_cancel")
async def on_refund_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Заявка на возврат отменена.")  # type: ignore
    await callback.answer()
