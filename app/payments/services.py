from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import PaymentStatus, ReferralStatus, SubscriptionStatus
from app.models.payment import Payment
from app.models.referral import Referral
from app.models.referral_transaction import ReferralTransaction
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.models.user import User
from app.payments.provider_base import PaymentResult
from app.services import subscription as subscription_service
from app.services.notification import notify_admin

logger = logging.getLogger(__name__)

_START_TARIFF_SLUG = "start"

# Maps AppSetting key → provider code used in registry
_PROVIDER_ENABLED_MAP: dict[str, str] = {
    "YOOKASSA_ENABLED":   "yookassa",
    "YOOMONEY_ENABLED":   "yoomoney",
    "FREEKASSA_ENABLED":  "freekassa",
    "LAVA_ENABLED":       "lava",
    "ROBOKASSA_ENABLED":  "robokassa",
    "PRODAMUS_ENABLED":   "prodamus",
    "CRYPTO_BOT_ENABLED": "crypto",
    "STARS_ENABLED":      "stars",
}

# Credential keys that MUST be non-empty for the provider to be usable
_PROVIDER_CRED_KEYS: dict[str, list[str]] = {
    "yookassa":  ["YOOKASSA_SHOP_ID", "YOOKASSA_SECRET_KEY"],
    "yoomoney":  ["YOOMONEY_WALLET", "YOOMONEY_NOTIFICATION_SECRET"],
    "freekassa": ["FREEKASSA_MERCHANT_ID", "FREEKASSA_SECRET_1", "FREEKASSA_SECRET_2"],
    "lava":      ["LAVA_PROJECT_ID", "LAVA_SECRET_KEY"],
    "robokassa": ["ROBOKASSA_MERCHANT_LOGIN", "ROBOKASSA_PASSWORD_1", "ROBOKASSA_PASSWORD_2"],
    "prodamus":  ["PRODAMUS_API_URL", "PRODAMUS_WEBHOOK_SECRET"],
    "crypto":    ["CRYPTO_BOT_TOKEN"],
    "stars":     [],   # no credentials needed — built into Telegram
    "manual":    [],
}


async def get_enabled_providers(session: AsyncSession) -> list[str]:
    """Return enabled provider codes that have all credentials configured."""
    from app.config import settings
    from app.models.app_setting import AppSetting

    all_keys = list(_PROVIDER_ENABLED_MAP.keys()) + [
        k for keys in _PROVIDER_CRED_KEYS.values() for k in keys
    ]
    result = await session.execute(
        select(AppSetting).where(AppSetting.key.in_(all_keys))
    )
    db_vals = {row.key: (row.value or "") for row in result.scalars().all()}

    def get_val(key: str) -> str:
        return db_vals.get(key) or getattr(settings, key, "")

    def has_credentials(provider: str) -> bool:
        return all(get_val(k).strip() for k in _PROVIDER_CRED_KEYS.get(provider, []))

    enabled = [
        code
        for key, code in _PROVIDER_ENABLED_MAP.items()
        if get_val(key).lower() in ("true", "1", "yes") and has_credentials(code)
    ]

    if not enabled:
        fallback = settings.PAYMENT_PROVIDER
        enabled = [fallback]

    return enabled


async def _is_first_payment(user_id: int, session: AsyncSession) -> bool:
    result = await session.execute(
        select(Payment).where(Payment.user_id == user_id, Payment.status == PaymentStatus.paid).limit(1)
    )
    return result.scalar_one_or_none() is None


async def create_payment(
    user_id: int,
    tariff_slug: str,
    session: AsyncSession,
    provider_name: str | None = None,
) -> tuple[Payment, PaymentResult]:
    from app.payments.registry import get_provider_db

    if provider_name is None:
        from app.config import settings
        provider_name = settings.PAYMENT_PROVIDER

    tariff_result = await session.execute(select(Tariff).where(Tariff.slug == tariff_slug))
    tariff = tariff_result.scalar_one()

    if tariff.price_first_month is not None:
        is_first = await _is_first_payment(user_id, session)
        amount = float(tariff.price_first_month if is_first else tariff.price_monthly)
    else:
        amount = float(tariff.price_monthly)

    # One subscription per user: reuse existing instead of creating a new one.
    # Order by id desc so we always pick the most recent (and most likely active) one.
    existing_result = await session.execute(
        select(Subscription).where(Subscription.user_id == user_id)
        .order_by(Subscription.id.desc()).limit(1)
    )
    existing = existing_result.scalar_one_or_none()

    if existing is not None:
        existing.tariff_id = tariff.id
        existing.updated_at = datetime.utcnow()
        # Don't touch active subscriptions (renewal path keeps them live until payment confirms).
        if existing.status not in (SubscriptionStatus.active, SubscriptionStatus.expires_soon):
            existing.status = SubscriptionStatus.pending_payment
        await session.flush()
        subscription = existing
    else:
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
        provider=provider_name,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(payment)
    await session.flush()

    provider = await get_provider_db(provider_name, session)
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
    # Commit payment as paid FIRST — so retried webhooks see it as already_processed
    # even if subscription activation below fails.
    await session.commit()
    logger.info("Payment %d committed as paid", payment.id)

    try:
        await _activate_from_payment(payment, session)
        await session.commit()
    except Exception as exc:
        logger.error(
            "Payment %d confirmed but activation failed (will need manual resend): %s",
            payment.id, exc, exc_info=True,
        )


async def _activate_from_payment(payment: Payment, session: AsyncSession) -> None:
    if payment.subscription_id is None:
        logger.error("Payment %d has no subscription_id", payment.id)
        return

    subscription = await session.get(Subscription, payment.subscription_id)
    if subscription is None:
        logger.error("No subscription for payment %d", payment.id)
        return

    tariff = await session.get(Tariff, payment.tariff_id)
    if tariff is None:
        logger.error("Tariff %d not found for payment %d", payment.tariff_id, payment.id)
        return

    from app.services.user_notifications import send_key_to_user, notify_payment_confirmed

    user = await session.get(User, payment.user_id)
    telegram_id = user.telegram_id if user else None

    if subscription.status in (SubscriptionStatus.active, SubscriptionStatus.expires_soon):
        # Renewal on an already-active subscription: just extend the expiry date.
        subscription.tariff_id = tariff.id
        await subscription_service.extend_subscription(subscription, tariff.duration_days, session)
        logger.info("Extended subscription %d by %d days (renewal)", subscription.id, tariff.duration_days)
        if telegram_id:
            await notify_payment_confirmed(telegram_id, float(payment.amount), is_renewal=True, subscription=subscription)
    else:
        # Fresh activation (pending_payment, expired, disabled, activation_error).
        subscription.status = SubscriptionStatus.pending_payment
        await subscription_service.activate_subscription(
            subscription, tariff, session, send_key_callback=send_key_to_user,
        )

    await _process_referral_reward(payment.user_id, session, payment=payment)


_REVSHARE_PERCENT = Decimal("0.10")
_MIN_REVSHARE_AMOUNT = Decimal("0.01")


async def _process_referral_reward(user_id: int, session: AsyncSession, payment: Payment | None = None) -> None:
    result = await session.execute(
        select(Referral).where(Referral.invited_user_id == user_id)
    )
    referral = result.scalar_one_or_none()
    if referral is None:
        return

    # First payment: award bonus days and mark referral as activated
    if referral.status == ReferralStatus.pending:
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

    # Rev-share: credit 10% of payment to referrer's balance on every payment
    if payment is not None and payment.amount and payment.amount >= _MIN_REVSHARE_AMOUNT:
        # Check this payment hasn't already been credited
        existing_tx = await session.execute(
            select(ReferralTransaction).where(ReferralTransaction.payment_id == payment.id)
        )
        if existing_tx.scalar_one_or_none() is None:
            earn = (payment.amount * _REVSHARE_PERCENT).quantize(Decimal("0.01"))
            referrer = await session.get(User, referral.referrer_id)
            if referrer is not None:
                referrer.referral_balance = (referrer.referral_balance or Decimal("0")) + earn
                tx = ReferralTransaction(
                    referrer_id=referral.referrer_id,
                    payer_user_id=user_id,
                    payment_id=payment.id,
                    amount_rub=earn,
                    payment_amount_rub=payment.amount,
                    created_at=datetime.utcnow(),
                )
                session.add(tx)
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
