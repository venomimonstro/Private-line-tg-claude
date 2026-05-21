from __future__ import annotations

import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.database import async_session_maker
from app.enums import PaymentStatus
from app.models.payment import Payment
from app.models.user import User

logger = logging.getLogger(__name__)

router = Router(name="payment_status")

_STATUS_EMOJI: dict[str, str] = {
    PaymentStatus.created: "🆕",
    PaymentStatus.pending: "⏳",
    PaymentStatus.manual_review: "🔍",
    PaymentStatus.paid: "✅",
    PaymentStatus.failed: "❌",
    PaymentStatus.cancelled: "🚫",
    PaymentStatus.refunded: "↩️",
}

_STATUS_LABEL: dict[str, str] = {
    PaymentStatus.created: "Создан",
    PaymentStatus.pending: "Ожидает оплаты",
    PaymentStatus.manual_review: "Ручная проверка",
    PaymentStatus.paid: "Оплачен",
    PaymentStatus.failed: "Ошибка",
    PaymentStatus.cancelled: "Отменён",
    PaymentStatus.refunded: "Возврат",
}


def _format_payment(payment: Payment) -> str:
    emoji = _STATUS_EMOJI.get(payment.status, "❓")
    label = _STATUS_LABEL.get(payment.status, payment.status)
    date_str = payment.created_at.strftime("%d.%m.%Y %H:%M")
    return (
        f"{emoji} <b>Платёж #{payment.id}</b>\n"
        f"   Сумма: {int(payment.amount)} ₽\n"
        f"   Статус: {label}\n"
        f"   Дата: {date_str}"
    )


@router.message(Command("payment_status"))
@router.message(F.text == "💳 Статус платежа")
async def cmd_payment_status(message: Message, db_user: User) -> None:
    """Show last 3 payments with their statuses."""
    async with async_session_maker() as session:
        result = await session.execute(
            select(Payment)
            .where(Payment.user_id == db_user.id)
            .order_by(Payment.created_at.desc())
            .limit(3)
        )
        payments = result.scalars().all()

    if not payments:
        await message.answer("У вас пока нет платежей.")
        return

    lines = ["<b>Ваши последние платежи:</b>\n"]
    for p in payments:
        lines.append(_format_payment(p))

    buttons = []
    for p in payments:
        if p.status == PaymentStatus.pending:
            buttons.append([
                InlineKeyboardButton(
                    text=f"🔄 Проверить #{p.id}",
                    callback_data=f"recheck_payment:{p.id}",
                )
            ])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

    await message.answer(
        "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=markup,
    )


@router.callback_query(F.data.startswith("recheck_payment:"))
async def on_recheck_payment(callback: CallbackQuery, db_user: User) -> None:
    payment_id_str = callback.data.split(":", 1)[1]  # type: ignore[union-attr]
    try:
        payment_id = int(payment_id_str)
    except ValueError:
        await callback.answer("Некорректный идентификатор платежа.", show_alert=True)
        return

    async with async_session_maker() as session:
        result = await session.execute(
            select(Payment).where(
                Payment.id == payment_id,
                Payment.user_id == db_user.id,
            )
        )
        payment = result.scalar_one_or_none()

        if payment is None:
            await callback.answer("Платёж не найден.", show_alert=True)
            return

        if payment.status != PaymentStatus.pending:
            status_label = _STATUS_LABEL.get(payment.status, payment.status)
            emoji = _STATUS_EMOJI.get(payment.status, "❓")
            await callback.answer(
                f"{emoji} Статус платежа: {status_label}",
                show_alert=True,
            )
            return

        # Try to verify with payment provider
        confirmed = False
        try:
            if payment.provider == "yookassa":
                from app.services.payment_verification import check_payment_status_yookassa
                confirmed = await check_payment_status_yookassa(payment, session)
            elif payment.provider == "lava":
                from app.services.payment_verification import check_payment_status_lava
                confirmed = await check_payment_status_lava(payment, session)
        except Exception as exc:
            logger.error("Recheck payment %d failed: %s", payment_id, exc)

        await session.commit()

    if confirmed:
        await callback.answer("✅ Платёж подтверждён! Ваша подписка активируется.", show_alert=True)
    else:
        status_label = _STATUS_LABEL.get(payment.status, payment.status)
        emoji = _STATUS_EMOJI.get(payment.status, "❓")
        await callback.answer(
            f"{emoji} Текущий статус: {status_label}\n"
            "Если платёж прошёл, он будет подтверждён автоматически в течение 15 минут.",
            show_alert=True,
        )
