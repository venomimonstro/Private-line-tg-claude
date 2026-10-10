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
    """Activate a promo trial only after a real VPN key is issued."""
    from app.models.tariff import Tariff
    from app.models.user import User
    from app.services import subscription as subscription_service
    from app.services.user_notifications import send_key_to_user

    # Serialize double taps / concurrent requests for the same account.
    locked_user = (await session.execute(
        select(User).where(User.id == user_id).with_for_update()
    )).scalar_one_or_none()
    if locked_user is None or await has_used_service(user_id, session):
        return None

    tariff = (await session.execute(
        select(Tariff).where(
            Tariff.slug == tariff_slug,
            Tariff.is_active == True,  # noqa: E712
        )
    )).scalar_one_or_none()
    if tariff is None:
        logger.error("Tariff slug %s not found for trial subscription", tariff_slug)
        return None

    trial_days = promo.trial_days or 7
    now = datetime.utcnow()
    subscription = Subscription(
        user_id=user_id,
        tariff_id=tariff.id,
        status=SubscriptionStatus.pending_payment,
        auto_renew=False,
        created_at=now,
        updated_at=now,
    )
    session.add(subscription)
    await session.flush()

    key = await subscription_service.activate_subscription(
        subscription,
        tariff,
        session,
        send_key_callback=send_key_to_user,
    )
    if key is None:
        logger.error(
            "Promo trial VPN activation failed for user %d tariff %s",
            user_id, tariff_slug,
        )
        return None

    subscription.expires_at = now + timedelta(days=trial_days)
    promo.uses_count = (promo.uses_count or 0) + 1
    await session.flush()

    logger.info(
        "Activated promo trial for user %d, tariff %s, %d days",
        user_id, tariff_slug, trial_days,
    )
    return subscription


async def activate_tariff_trial(
    user_id: int,
    tariff_slug: str,
    session: AsyncSession,
) -> Subscription | None:
    """Activate built-in trial only after a production VPN key is issued."""
    from app.models.tariff import Tariff
    from app.models.user import User
    from app.services import subscription as subscription_service
    from app.services.user_notifications import send_key_to_user

    locked_user = (await session.execute(
        select(User).where(User.id == user_id).with_for_update()
    )).scalar_one_or_none()
    if locked_user is None or await has_used_service(user_id, session):
        return None

    tariff = (await session.execute(
        select(Tariff).where(
            Tariff.slug == tariff_slug,
            Tariff.is_active == True,  # noqa: E712
        )
    )).scalar_one_or_none()
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

    key = await subscription_service.activate_subscription(
        sub, tariff, session, send_key_callback=send_key_to_user,
    )
    if key is None:
        logger.error(
            "Built-in trial VPN activation failed for user %d tariff %s",
            user_id, tariff_slug,
        )
        return None

    sub.expires_at = now + timedelta(days=tariff.trial_days)
    await session.flush()
    logger.info(
        "Activated built-in trial for user %d, tariff %s, %d days",
        user_id, tariff_slug, tariff.trial_days,
    )
    return sub

