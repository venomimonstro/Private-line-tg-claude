from __future__ import annotations

import logging
import math

from aiogram import Router
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery

from app.database import async_session_maker
from app.enums import PaymentStatus
from app.models.payment import Payment
from app.payments.services import confirm_payment_auto

router = Router(name="stars_payment")
logger = logging.getLogger(__name__)


async def send_stars_invoice(
    bot: object,
    user_id: int,
    payment_id: int,
    amount: float,
    description: str,
) -> None:
    from app.config import settings

    stars = max(1, math.ceil(amount * settings.STARS_PER_RUB))
    await bot.send_invoice(  # type: ignore[attr-defined]
        chat_id=user_id,
        title="Private Line",
        description=description,
        payload=f"pl_payment_{payment_id}",
        currency="XTR",
        prices=[LabeledPrice(label="Оплата", amount=stars)],
    )


@router.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(lambda m: m.successful_payment is not None)
async def process_successful_payment(message: Message) -> None:
    sp = message.successful_payment
    if sp is None or not sp.invoice_payload.startswith("pl_payment_"):
        return
    try:
        payment_id = int(sp.invoice_payload.split("_")[-1])
    except ValueError:
        return

    async with async_session_maker() as session:
        payment = await session.get(Payment, payment_id)
        if payment is None or payment.status != PaymentStatus.pending:
            return
        payment.provider_payment_id = sp.telegram_payment_charge_id
        await confirm_payment_auto(payment, session)

    await message.answer("Оплата звёздами прошла успешно! Ваш доступ активирован.")
