from __future__ import annotations

import logging
import math

from aiogram import Router
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery, SuccessfulPayment

from app.database import async_session_maker
from app.enums import PaymentStatus
from app.models.payment import Payment
from app.models.user import User
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


def _parse_payment_id(payload: str) -> int | None:
    if payload.startswith(_PAYLOAD_PREFIX_LEGACY):
        raw = payload[len(_PAYLOAD_PREFIX_LEGACY):]
    elif payload.startswith(_PAYLOAD_PREFIX):
        raw = payload[len(_PAYLOAD_PREFIX):]
    else:
        return None
    try:
        value = int(raw)
        return value if value > 0 else None
    except ValueError:
        return None


async def _validate_stars_order(
    payment_id: int,
    telegram_id: int,
    total_amount: int,
    currency: str,
) -> bool:
    from app.config import settings

    async with async_session_maker() as session:
        payment = await session.get(Payment, payment_id)
        if payment is None or payment.provider != "stars":
            return False
        user = await session.get(User, payment.user_id)
        if user is None or user.telegram_id != telegram_id:
            return False
        if payment.status not in (PaymentStatus.pending, PaymentStatus.paid):
            return False
        expected = max(1, math.ceil(float(payment.amount) * settings.STARS_PER_RUB))
        return currency == XTR and total_amount == expected


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    payment_id = _parse_payment_id(query.invoice_payload)
    ok = (
        payment_id is not None
        and await _validate_stars_order(
            payment_id,
            query.from_user.id,
            query.total_amount,
            query.currency,
        )
    )
    await query.answer(
        ok=ok,
        error_message=None if ok else "Платёж не соответствует заказу. Создайте счёт заново.",
    )


@router.message(lambda m: m.successful_payment is not None)
async def on_successful_payment(message: Message) -> None:
    sp: SuccessfulPayment | None = message.successful_payment
    if sp is None:
        return

    payload = sp.invoice_payload
    our_payment_id = _parse_payment_id(payload)
    if our_payment_id is None or message.from_user is None:
        return

    if not await _validate_stars_order(
        our_payment_id,
        message.from_user.id,
        sp.total_amount,
        sp.currency,
    ):
        logger.error("Stars payment invariant mismatch for payment %s", our_payment_id)
        return

    async with async_session_maker() as session:
        payment = await session.get(Payment, our_payment_id)
        if payment is None:
            logger.error("Stars payment: payment %d not found", our_payment_id)
            return
        if payment.status == PaymentStatus.paid and payment.fulfilled_at is not None:
            logger.info("Stars payment %d duplicate success ignored", our_payment_id)
            return
        if payment.status not in (PaymentStatus.pending, PaymentStatus.paid):
            logger.error(
                "Stars payment %d has non-payable status %s",
                our_payment_id, payment.status,
            )
            return
        if (
            payment.provider_payment_id
            and payment.provider_payment_id != sp.telegram_payment_charge_id
            and payment.status == PaymentStatus.paid
        ):
            logger.error("Stars charge ID mismatch for paid payment %d", our_payment_id)
            return
        payment.provider_payment_id = sp.telegram_payment_charge_id
        await confirm_payment_auto(payment, session)

    await message.answer(
        "✅ Оплата через Telegram Stars прошла успешно!\n"
        "Ваш персональный ключ доступа будет отправлен в ближайшее время."
    )
