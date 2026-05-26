from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import SubscriptionStatus
from app.models.promo_code import PromoCode
from app.models.subscription import Subscription

logger = logging.getLogger(__name__)

# Statuses that mean the user actually received VPN service.
# disabled/blocked/activation_error/pending_payment do NOT block a trial.
_USED_SERVICE_STATUSES = (
    SubscriptionStatus.active,
    SubscriptionStatus.expires_soon,
    SubscriptionStatus.expired,
)


async def has_used_service(user_id: int, session: AsyncSession) -> bool:
    """Return True only if the user ever had an active/expired subscription."""
    r = await session.execute(
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.status.in_(_USED_SERVICE_STATUSES),
        )
        .limit(1)
    )
    return r.scalar_one_or_none() is not None


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
        if await has_used_service(user_id, session):
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

    if await has_used_service(user_id, session):
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


async def activate_tariff_trial(
    user_id: int,
    tariff_slug: str,
    session: AsyncSession,
) -> Subscription | None:
    """Activate built-in tariff trial (no promo code). Returns subscription or None if ineligible."""
    from app.models.tariff import Tariff
    from app.services import subscription as subscription_service
    from app.services.user_notifications import send_key_to_user

    if await has_used_service(user_id, session):
        return None

    tariff_result = await session.execute(
        select(Tariff).where(Tariff.slug == tariff_slug, Tariff.is_active == True)  # noqa: E712
    )
    tariff = tariff_result.scalar_one_or_none()
    if tariff is None or not tariff.trial_days:
        return None

    now = datetime.utcnow()
    sub = Subscription(
        user_id=user_id,
        tariff_id=tariff.id,
        status=SubscriptionStatus.pending_payment,
        auto_renew=False,
        created_at=now,
        updated_at=now,
    )
    session.add(sub)
    await session.flush()

    await subscription_service.activate_subscription(
        sub, tariff, session, send_key_callback=send_key_to_user,
    )

    # Override expiry to trial_days (activate_subscription uses tariff.duration_days)
    if sub.status == SubscriptionStatus.active:
        sub.expires_at = now + timedelta(days=tariff.trial_days)
        await session.flush()

    logger.info("Activated built-in trial for user %d, tariff %s, %d days", user_id, tariff_slug, tariff.trial_days)
    return sub
