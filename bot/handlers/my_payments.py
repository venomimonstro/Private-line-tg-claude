from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session_maker
from app.enums import PaymentStatus
from app.models.payment import Payment
from app.models.user import User

router = Router(name="my_payments")

_STATUS_EMOJI = {
    PaymentStatus.created:       "🆕",
    PaymentStatus.pending:       "⏳",
    PaymentStatus.manual_review: "🔍",
    PaymentStatus.paid:          "✅",
    PaymentStatus.failed:        "❌",
    PaymentStatus.cancelled:     "🚫",
    PaymentStatus.refunded:      "↩️",
}

_STATUS_LABEL = {
    PaymentStatus.created:       "Создан",
    PaymentStatus.pending:       "Ожидает",
    PaymentStatus.manual_review: "На проверке",
    PaymentStatus.paid:          "Оплачен",
    PaymentStatus.failed:        "Ошибка",
    PaymentStatus.cancelled:     "Отменён",
    PaymentStatus.refunded:      "Возврат",
}


@router.message(F.text == "💳 Мои платежи")
async def my_payments(message: Message, db_user: User) -> None:
    async with async_session_maker() as session:
        result = await session.execute(
            select(Payment)
            .where(Payment.user_id == db_user.id)
            .options(
                selectinload(Payment.tariff),
                selectinload(Payment.subscription),
            )
            .order_by(Payment.created_at.desc())
            .limit(10)
        )
        payments = result.scalars().all()

    if not payments:
        await message.answer("У вас пока нет платежей.")
        return

    lines = ["<b>📋 История платежей (последние 10):</b>\n"]
    for p in payments:
        emoji = _STATUS_EMOJI.get(p.status, "❓")
        label = _STATUS_LABEL.get(p.status, p.status)
        tariff_name = p.tariff.name if p.tariff else "—"
        pay_date = p.created_at.strftime("%d.%m.%Y")

        period = ""
        if p.confirmed_at and p.subscription:
            act = p.confirmed_at.strftime("%d.%m.%Y")
            exp = p.subscription.expires_at.strftime("%d.%m.%Y") if p.subscription.expires_at else "—"
            period = f"\n   📅 {act} → {exp}"

        lines.append(
            f"{emoji} <b>{pay_date}</b> — {tariff_name}\n"
            f"   💰 {int(p.amount)} ₽  |  {label}{period}"
        )

    await message.answer("\n\n".join(lines), parse_mode="HTML")
