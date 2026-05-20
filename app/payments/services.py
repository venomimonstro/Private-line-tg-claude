from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import PaymentStatus, ReferralStatus, SubscriptionStatus
from app.models.payment import Payment
from app.models.referral import Referral
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.services import subscription as subscription_service
from app.services.notification import notify_admin

logger = logging.getLogger(__name__)

_START_TARIFF_FIRST_MONTH_PRICE = 99.0
_START_TARIFF_CODE = "start"


async def _get_first_month_price(user_id: int, tariff_code: str, session: AsyncSession) -> float:
    """Return 99₽ for «Старт» if user has no previous paid subscription."""
    if tariff_code != _START_TARIFF_CODE:
        result = await session.execute(select(Tariff).where(Tariff.code == tariff_code))
        tariff = result.scalar_one()
        return float(tariff.price)

    previous = await session.execute(
        select(Payment).where(
            Payment.user_id == user_id,
            Payment.status == PaymentStatus.paid,
        )
    )
    has_previous = previous.scalar_one_or_none() is not None
    result = await session.execute(select(Tariff).where(Tariff.code == tariff_code))
    tariff = result.scalar_one()
    return float(tariff.price) if has_previous else _START_TARIFF_FIRST_MONTH_PRICE


async def create_payment(
    user_id: int,
    tariff_code: str,
    session: AsyncSession,
) -> Payment:
    tariff_result = await session.execute(select(Tariff).where(Tariff.code == tariff_code))
    tariff = tariff_result.scalar_one()

    amount = await _get_first_month_price(user_id, tariff_code, session)

    subscription = Subscription(
        user_id=user_id,
        tariff_code=tariff_code,
        status=SubscriptionStatus.pending_payment,
        traffic_limit_gb=tariff.traffic_limit_gb,
        devices_limit=tariff.devices_limit,
    )
    session.add(subscription)
    await session.flush()

    payment = Payment(
        user_id=user_id,
        tariff_code=tariff_code,
        amount=amount,
        currency=tariff.currency,
        status=PaymentStatus.pending,
        provider="manual",
    )
    session.add(payment)
    await session.flush()

    await notify_admin(
        f"💳 Новая заявка на оплату #{payment.id}\n"
        f"Пользователь: {user_id}\n"
        f"Тариф: {tariff.name}\n"
        f"Сумма: {amount} {tariff.currency}"
    )
    return payment


async def confirm_payment(
    payment: Payment,
    admin_id: int,
    session: AsyncSession,
    send_key_callback: object | None = None,
) -> None:
    payment.status = PaymentStatus.paid
    payment.confirmed_at = datetime.utcnow()
    payment.confirmed_by_admin_id = admin_id
    await session.flush()

    sub_result = await session.execute(
        select(Subscription).where(
            Subscription.user_id == payment.user_id,
            Subscription.tariff_code == payment.tariff_code,
            Subscription.status == SubscriptionStatus.pending_payment,
        ).order_by(Subscription.created_at.desc())
    )
    subscription = sub_result.scalars().first()

    if subscription is None:
        logger.error("No pending subscription found for payment %s", payment.id)
        return

    await subscription_service.activate_subscription(subscription, session, send_key_callback)
    await session.flush()

    await _process_referral_reward(payment.user_id, session)
    await session.commit()


async def _process_referral_reward(user_id: int, session: AsyncSession) -> None:
    referral_result = await session.execute(
        select(Referral).where(
            Referral.invited_user_id == user_id,
            Referral.status == ReferralStatus.pending,
        )
    )
    referral = referral_result.scalar_one_or_none()
    if referral is None:
        return

    referrer_sub_result = await session.execute(
        select(Subscription).where(
            Subscription.user_id == referral.referrer_user_id,
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
        ).order_by(Subscription.expires_at.desc())
    )
    referrer_sub = referrer_sub_result.scalars().first()

    if referrer_sub is not None:
        await subscription_service.extend_subscription(referrer_sub, referral.reward_days, session)

    referral.status = ReferralStatus.activated
    referral.activated_at = datetime.utcnow()
    await session.flush()


async def cancel_payment(payment: Payment, admin_id: int, session: AsyncSession) -> None:
    payment.status = PaymentStatus.cancelled
    await session.flush()

    sub_result = await session.execute(
        select(Subscription).where(
            Subscription.user_id == payment.user_id,
            Subscription.tariff_code == payment.tariff_code,
            Subscription.status == SubscriptionStatus.pending_payment,
        ).order_by(Subscription.created_at.desc())
    )
    subscription = sub_result.scalars().first()
    if subscription is not None:
        subscription.status = SubscriptionStatus.disabled

    await session.commit()
