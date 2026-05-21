from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import PaymentStatus, ReferralStatus, SubscriptionStatus
from app.models.payment import Payment
from app.models.referral import Referral
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.payments.provider_base import PaymentResult
from app.services import subscription as subscription_service
from app.services.notification import notify_admin

logger = logging.getLogger(__name__)

_START_TARIFF_SLUG = "start"


async def _is_first_payment(user_id: int, session: AsyncSession) -> bool:
    result = await session.execute(
        select(Payment).where(Payment.user_id == user_id, Payment.status == PaymentStatus.paid).limit(1)
    )
    return result.scalar_one_or_none() is None


async def create_payment(
    user_id: int,
    tariff_slug: str,
    session: AsyncSession,
) -> tuple[Payment, PaymentResult]:
    from app.payments.registry import get_provider

    tariff_result = await session.execute(select(Tariff).where(Tariff.slug == tariff_slug))
    tariff = tariff_result.scalar_one()

    # Determine price (first month discount for "start" tariff)
    if tariff.slug == _START_TARIFF_SLUG and tariff.price_first_month is not None:
        is_first = await _is_first_payment(user_id, session)
        amount = float(tariff.price_first_month if is_first else tariff.price_monthly)
    else:
        amount = float(tariff.price_monthly)

    subscription = Subscription(
        user_id=user_id,
        tariff_id=tariff.id,
        status=SubscriptionStatus.pending_payment,
        auto_renew=False,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(subscription)
    await session.flush()

    payment = Payment(
        user_id=user_id,
        subscription_id=subscription.id,
        tariff_id=tariff.id,
        amount=Decimal(str(amount)),
        currency="RUB",
        status=PaymentStatus.pending,
        provider=_provider_name(),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(payment)
    await session.flush()

    provider = get_provider()
    description = f"Private Line — {tariff.name}"
    payment_result = await provider.create_payment(
        our_payment_id=payment.id,
        amount=amount,
        currency="RUB",
        description=description,
        user_id=user_id,
    )

    if payment_result.provider_payment_id and payment_result.provider_payment_id != "manual":
        payment.provider_payment_id = payment_result.provider_payment_id

    await notify_admin(
        f"💳 Новая заявка #{payment.id}\n"
        f"Пользователь: {user_id}\n"
        f"Тариф: {tariff.name}\n"
        f"Сумма: {amount} RUB\n"
        f"Провайдер: {payment.provider}"
    )
    return payment, payment_result


def _provider_name() -> str:
    from app.config import settings
    return settings.PAYMENT_PROVIDER


async def confirm_payment(
    payment: Payment,
    admin_id: int,
    session: AsyncSession,
) -> None:
    """Confirm payment by admin (manual). Caller must commit."""
    payment.status = PaymentStatus.paid
    payment.confirmed_at = datetime.utcnow()
    payment.confirmed_by_id = admin_id
    payment.updated_at = datetime.utcnow()
    await session.flush()

    await _activate_from_payment(payment, session)


async def confirm_payment_auto(
    payment: Payment,
    session: AsyncSession,
) -> None:
    """Auto-confirm from webhook/stars. Commits itself."""
    payment.status = PaymentStatus.paid
    payment.confirmed_at = datetime.utcnow()
    payment.updated_at = datetime.utcnow()
    await session.flush()

    await _activate_from_payment(payment, session)
    await session.commit()


async def _activate_from_payment(payment: Payment, session: AsyncSession) -> None:
    if payment.subscription_id is None:
        logger.error("Payment %d has no subscription_id", payment.id)
        return

    subscription = await session.get(Subscription, payment.subscription_id)
    if subscription is None or subscription.status != SubscriptionStatus.pending_payment:
        logger.error("No pending subscription for payment %d", payment.id)
        return

    # Load tariff for duration info
    tariff = await session.get(Tariff, payment.tariff_id)
    if tariff is None:
        logger.error("Tariff %d not found for payment %d", payment.tariff_id, payment.id)
        return

    await subscription_service.activate_subscription(subscription, tariff, session)
    await _process_referral_reward(payment.user_id, session)


async def _process_referral_reward(user_id: int, session: AsyncSession) -> None:
    result = await session.execute(
        select(Referral).where(
            Referral.invited_user_id == user_id,
            Referral.status == ReferralStatus.pending,
        )
    )
    referral = result.scalar_one_or_none()
    if referral is None:
        return

    referrer_sub_result = await session.execute(
        select(Subscription).where(
            Subscription.user_id == referral.referrer_id,
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
        ).order_by(Subscription.expires_at.desc())
    )
    sub = referrer_sub_result.scalars().first()
    if sub is not None:
        await subscription_service.extend_subscription(sub, referral.bonus_days_awarded, session)

    referral.status = ReferralStatus.activated
    referral.activated_at = datetime.utcnow()
    await session.flush()


async def cancel_payment(payment: Payment, admin_id: int, session: AsyncSession) -> None:
    """Cancel payment. Caller must commit."""
    payment.status = PaymentStatus.cancelled
    payment.updated_at = datetime.utcnow()
    await session.flush()

    if payment.subscription_id is not None:
        sub = await session.get(Subscription, payment.subscription_id)
        if sub is not None and sub.status == SubscriptionStatus.pending_payment:
            sub.status = SubscriptionStatus.disabled
            sub.updated_at = datetime.utcnow()
