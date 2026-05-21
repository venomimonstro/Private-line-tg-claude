from __future__ import annotations

import logging
import math

from aiogram import Router
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery, SuccessfulPayment

from app.database import async_session_maker
from app.enums import PaymentStatus
from app.models.payment import Payment
from app.payments.services import confirm_payment_auto

router = Router(name="stars_payment")
logger = logging.getLogger(__name__)

XTR = "XTR"  # Telegram Stars currency code

# Payload prefixes (both supported for backward compat)
_PAYLOAD_PREFIX = "pl_"
_PAYLOAD_PREFIX_LEGACY = "pl_payment_"


async def send_stars_invoice(
    bot: object,
    user_id: int,
    payment_id: int,
    amount: float,
    description: str,
) -> None:
    """Send Telegram Stars invoice. Stars = amount * STARS_PER_RUB."""
    from app.config import settings

    stars = max(1, math.ceil(amount * settings.STARS_PER_RUB))
    await bot.send_invoice(  # type: ignore[attr-defined]
        chat_id=user_id,
        title="Private Line",
        description=description,
        payload=f"{_PAYLOAD_PREFIX}{payment_id}",
        currency=XTR,
        prices=[LabeledPrice(label=description, amount=stars)],
    )


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(lambda m: m.successful_payment is not None)
async def on_successful_payment(message: Message) -> None:
    sp: SuccessfulPayment | None = message.successful_payment
    if sp is None:
        return

    payload = sp.invoice_payload
    our_payment_id: int | None = None

    # Support both legacy and new payload formats
    if payload.startswith(_PAYLOAD_PREFIX_LEGACY):
        try:
            our_payment_id = int(payload[len(_PAYLOAD_PREFIX_LEGACY):])
        except ValueError:
            return
    elif payload.startswith(_PAYLOAD_PREFIX):
        try:
            our_payment_id = int(payload[len(_PAYLOAD_PREFIX):])
        except ValueError:
            return
    else:
        return

    async with async_session_maker() as session:
        payment = await session.get(Payment, our_payment_id)
        if payment is None:
            logger.error("Stars payment: payment %d not found", our_payment_id)
            return
        if payment.status != PaymentStatus.pending:
            return
        payment.provider_payment_id = sp.telegram_payment_charge_id
        await confirm_payment_auto(payment, session)

    await message.answer(
        "✅ Оплата через Telegram Stars прошла успешно!\n"
        "Ваш персональный ключ доступа будет отправлен в ближайшее время."
    )
