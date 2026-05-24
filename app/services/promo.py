from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import PaymentStatus, SubscriptionStatus
from app.models.promo_code import PromoCode
from app.models.subscription import Subscription

logger = logging.getLogger(__name__)


async def validate_promo(
    code: str,
    tariff_slug: str,
    user_id: int,
    session: AsyncSession,
) -> tuple[PromoCode | None, str]:
    """Returns (promo, error_msg). error_msg is empty if valid."""
    result = await session.execute(
        select(PromoCode).where(func.upper(PromoCode.code) == code.upper())
    )
    promo = result.scalar_one_or_none()

    if promo is None:
        return None, "Промокод не найден."

    if not promo.is_active:
        return None, "Промокод неактивен."

    if promo.expires_at is not None and promo.expires_at < datetime.utcnow():
        return None, "Срок действия промокода истёк."

    if promo.uses_max is not None and promo.uses_count >= promo.uses_max:
        return None, "Промокод уже использован максимальное количество раз."

    if promo.tariff_slug is not None and promo.tariff_slug != tariff_slug:
        return None, f"Промокод действителен только для тарифа «{promo.tariff_slug}»."

    # Check if user already used this promo code
    # For trial promos — check subscriptions linked to this promo
    # For discount promos — check payments with matching promo
    # We track usage via uses_count, but also need to prevent per-user reuse.
    # Simple approach: check if user already has a subscription that started
    # around when this promo was used (not ideal). Instead, check via a
    # dedicated payment metadata field if available. For now, check trial
    # subscriptions by checking if user ever had trial sub for same tariff.
    if promo.is_trial:
        trial_result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.status.in_([
                    SubscriptionStatus.active,
                    SubscriptionStatus.expires_soon,
                    SubscriptionStatus.expired,
                ]),
            ).limit(1)
        )
        if trial_result.scalar_one_or_none() is not None:
            return None, "Пробный период доступен только для новых пользователей."

    return promo, ""


async def apply_promo(promo: PromoCode, amount: float, session: AsyncSession) -> float:
    """Returns discounted amount. Increments uses_count."""
    discounted = amount

    if promo.discount_percent is not None:
        discounted = amount * (1 - promo.discount_percent / 100.0)
    elif promo.discount_fixed is not None:
        discounted = amount - float(promo.discount_fixed)

    # Minimum amount: 1.0 RUB
    discounted = max(1.0, discounted)

    # Increment usage counter
    promo.uses_count = (promo.uses_count or 0) + 1
    await session.flush()

    return discounted


async def create_trial_subscription(
    user_id: int,
    tariff_slug: str,
    promo: PromoCode,
    session: AsyncSession,
) -> Subscription | None:
    """Creates a free trial subscription. Returns None if user already had trial."""
    from datetime import timedelta
    from app.models.subscription import Subscription
    from app.models.tariff import Tariff

    # Trial is only for users who have never had any subscription.
    result = await session.execute(
        select(Subscription).where(Subscription.user_id == user_id).limit(1)
    )
    if result.scalar_one_or_none() is not None:
        return None

    tariff_result = await session.execute(
        select(Tariff).where(Tariff.slug == tariff_slug)
    )
    tariff = tariff_result.scalar_one_or_none()
    if tariff is None:
        logger.error("Tariff slug %s not found for trial subscription", tariff_slug)
        return None

    trial_days = promo.trial_days or 7
    now = datetime.utcnow()

    subscription = Subscription(
        user_id=user_id,
        tariff_id=tariff.id,
        status=SubscriptionStatus.active,
        started_at=now,
        expires_at=now + timedelta(days=trial_days),
        auto_renew=False,
        created_at=now,
        updated_at=now,
    )
    session.add(subscription)

    # Increment promo uses_count
    promo.uses_count = (promo.uses_count or 0) + 1
    await session.flush()

    logger.info(
        "Created trial subscription for user %d, tariff %s, %d days",
        user_id, tariff_slug, trial_days,
    )
    return subscription
