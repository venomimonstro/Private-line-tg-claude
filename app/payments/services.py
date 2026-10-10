from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import PaymentStatus, ReferralStatus, SubscriptionStatus, UserStatus
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
        select(Payment).where(
            Payment.user_id == user_id,
            Payment.status == PaymentStatus.paid,
        ).limit(1)
    )
    return result.scalar_one_or_none() is None


async def calculate_payment_amount(
    user_id: int,
    tariff: Tariff,
    session: AsyncSession,
) -> Decimal:
    """Single source of truth for the base amount before an optional promo."""
    if tariff.price_first_month is not None and await _is_first_payment(user_id, session):
        value = Decimal(str(tariff.price_first_month))
    else:
        value = Decimal(str(tariff.price_monthly))
    return value.quantize(Decimal("0.01"))


async def create_payment(
    user_id: int,
    tariff_slug: str,
    session: AsyncSession,
    provider_name: str | None = None,
    amount_override: float | Decimal | None = None,
    reuse_existing_subscription: bool = True,
) -> tuple[Payment, PaymentResult]:
    from app.payments.registry import get_provider_db

    if provider_name is None:
        from app.config import settings
        provider_name = settings.PAYMENT_PROVIDER

    tariff_result = await session.execute(select(Tariff).where(Tariff.slug == tariff_slug))
    tariff = tariff_result.scalar_one()

    base_amount = await calculate_payment_amount(user_id, tariff, session)
    if amount_override is None:
        amount_dec = base_amount
    else:
        amount_dec = Decimal(str(amount_override)).quantize(Decimal("0.01"))
        if amount_dec < Decimal("1.00") or amount_dec > base_amount:
            raise ValueError("Invalid payment amount override")
    amount = float(amount_dec)

    # Reuse the actual live subscription for renewal/plan purchase, but do
    # NOT mutate its tariff/status until the payment is confirmed. Gifts use a
    # dedicated placeholder so the giver's live subscription is never touched.
    existing = None
    if reuse_existing_subscription:
        existing = (await session.execute(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.status.in_([
                    SubscriptionStatus.active,
                    SubscriptionStatus.expires_soon,
                ]),
            ).order_by(Subscription.expires_at.desc(), Subscription.id.desc()).limit(1)
        )).scalar_one_or_none()
        if existing is None:
            existing = (await session.execute(
                select(Subscription).where(Subscription.user_id == user_id)
                .order_by(Subscription.id.desc()).limit(1)
            )).scalar_one_or_none()

    if existing is not None:
        subscription = existing
        if existing.status not in (
            SubscriptionStatus.active,
            SubscriptionStatus.expires_soon,
        ):
            existing.tariff_id = tariff.id
            existing.status = SubscriptionStatus.pending_payment
            existing.updated_at = datetime.utcnow()
            await session.flush()
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
        amount=amount_dec,
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
    if payment_result.payment_url:
        payment.payment_url = payment_result.payment_url

    await notify_admin(
        f"💳 Новая заявка #{payment.id}\n"
        f"Пользователь: {user_id}\n"
        f"Тариф: {tariff.name}\n"
        f"Сумма: {amount} RUB\n"
        f"Провайдер: {payment.provider}"
    )
    return payment, payment_result


async def _locked_payment(payment_id: int, session: AsyncSession) -> Payment:
    locked = (await session.execute(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )).scalar_one_or_none()
    if locked is None:
        raise RuntimeError(f"Payment {payment_id} disappeared")
    return locked


async def _fulfill_paid_payment(payment_id: int, session: AsyncSession) -> bool:
    """Exactly-once subscription fulfillment for an already-paid payment.

    Returns True when this call performed fulfillment, False if another worker
    already completed it. The payment row lock serializes webhook, polling, and
    recovery workers.
    """
    payment = await _locked_payment(payment_id, session)
    if payment.status != PaymentStatus.paid:
        raise RuntimeError(f"Payment {payment.id} is not paid")
    if payment.fulfilled_at is not None:
        logger.info("Payment %d already fulfilled; duplicate event ignored", payment.id)
        return False

    await _activate_from_payment(payment, session)
    payment.fulfilled_at = datetime.utcnow()
    payment.updated_at = datetime.utcnow()
    await session.flush()
    return True


async def confirm_payment(
    payment: Payment,
    admin_id: int,
    session: AsyncSession,
) -> None:
    """Confirm a manual payment exactly once. Caller commits."""
    payment = await _locked_payment(payment.id, session)
    if payment.fulfilled_at is not None:
        return
    if payment.status != PaymentStatus.paid:
        payment.status = PaymentStatus.paid
        payment.confirmed_at = datetime.utcnow()
        payment.confirmed_by_id = admin_id
        payment.updated_at = datetime.utcnow()
        await session.flush()
    await _fulfill_paid_payment(payment.id, session)


async def confirm_payment_auto(
    payment: Payment,
    session: AsyncSession,
) -> None:
    """Persist provider confirmation, then fulfill exactly once.

    Paid status is committed before external VPN provisioning so provider
    retries are idempotent. fulfilled_at remains NULL until subscription/VPN
    provisioning has completed and becomes the durable recovery marker.
    """
    payment_id = payment.id
    locked = await _locked_payment(payment_id, session)
    if locked.fulfilled_at is not None:
        logger.info("Payment %d duplicate confirmation ignored (already fulfilled)", payment_id)
        return

    if locked.status != PaymentStatus.paid:
        locked.status = PaymentStatus.paid
        locked.confirmed_at = locked.confirmed_at or datetime.utcnow()
        locked.updated_at = datetime.utcnow()
        await session.flush()
        await session.commit()
        logger.info("Payment %d durably committed as paid; fulfillment pending", payment_id)

    try:
        performed = await _fulfill_paid_payment(payment_id, session)
        await session.commit()
        if performed:
            logger.info("Payment %d fulfillment committed", payment_id)
    except Exception as exc:
        # Keep PAID + fulfilled_at=NULL as a recoverable durable state. Changes
        # such as activation_error made before the exception are useful to the
        # recovery worker, so persist them when possible.
        logger.error(
            "Payment %d paid but fulfillment failed; recovery remains pending: %s",
            payment_id, type(exc).__name__, exc_info=True,
        )
        try:
            await session.commit()
        except Exception:
            await session.rollback()


async def recover_paid_unfulfilled_payments(
    session: AsyncSession,
    *,
    limit: int = 50,
) -> dict[str, int]:
    """Finish paid payments left between provider confirmation and fulfillment."""
    rows = (await session.execute(
        select(Payment.id)
        .where(
            Payment.status == PaymentStatus.paid,
            Payment.fulfilled_at.is_(None),
        )
        .order_by(Payment.confirmed_at.asc().nullsfirst(), Payment.id.asc())
        .limit(limit)
    )).scalars().all()

    result = {"checked": len(rows), "fulfilled": 0, "failed": 0}
    for payment_id in rows:
        try:
            performed = await _fulfill_paid_payment(payment_id, session)
            await session.commit()
            if performed:
                result["fulfilled"] += 1
        except Exception as exc:
            result["failed"] += 1
            logger.error(
                "Paid payment %d recovery failed: %s",
                payment_id, type(exc).__name__, exc_info=True,
            )
            try:
                await session.commit()
            except Exception:
                await session.rollback()
    return result


async def _activate_from_payment(payment: Payment, session: AsyncSession) -> None:
    if payment.subscription_id is None:
        raise RuntimeError(f"Payment {payment.id} has no subscription_id")

    subscription = await session.get(Subscription, payment.subscription_id)
    if subscription is None:
        raise RuntimeError(f"No subscription for payment {payment.id}")

    tariff = await session.get(Tariff, payment.tariff_id)
    if tariff is None:
        raise RuntimeError(f"Tariff {payment.tariff_id} not found for payment {payment.id}")

    from app.services.user_notifications import notify_gift_sent, notify_payment_confirmed, send_key_to_user

    user = await session.get(User, payment.user_id)
    telegram_id = user.telegram_id if user else None

    # ── Gift subscription ──────────────────────────────────────────────────────
    if payment.gift_recipient_telegram_id:
        recipient_result = await session.execute(
            select(User).where(User.telegram_id == payment.gift_recipient_telegram_id)
        )
        recipient = recipient_result.scalar_one_or_none()
        if recipient is None:
            recipient = User(
                telegram_id=payment.gift_recipient_telegram_id,
                source="gift",
                status=UserStatus.active,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                last_activity_at=datetime.utcnow(),
            )
            session.add(recipient)
            await session.flush()

        payment.gift_recipient_id = recipient.id

        # On the first fulfillment attempt payment.subscription_id still points
        # at the giver's placeholder. Switch it durably to the recipient's
        # subscription so a crash/retry cannot create duplicate gift subs.
        if subscription.user_id != recipient.id:
            subscription.status = SubscriptionStatus.disabled
            subscription.updated_at = datetime.utcnow()
            gift_sub = Subscription(
                user_id=recipient.id,
                tariff_id=tariff.id,
                status=SubscriptionStatus.pending_payment,
                auto_renew=False,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            session.add(gift_sub)
            await session.flush()
            payment.subscription_id = gift_sub.id
            subscription = gift_sub
        else:
            gift_sub = subscription

        if gift_sub.status not in (
            SubscriptionStatus.active,
            SubscriptionStatus.expires_soon,
        ):
            gift_key = await subscription_service.activate_subscription(
                gift_sub, tariff, session, send_key_callback=send_key_to_user,
            )
            if gift_key is None:
                raise RuntimeError(f"Gift payment {payment.id} VPN activation failed")

        await notify_admin(
            f"🎁 Подарочная подписка #{payment.id}\n"
            f"Даритель: {payment.user_id}\n"
            f"Получатель TG: {payment.gift_recipient_telegram_id}\n"
            f"Тариф: {tariff.name}"
        )
        if telegram_id:
            await notify_gift_sent(
                telegram_id, payment.gift_recipient_telegram_id, tariff.name
            )
        return

    # ── Normal activation / renewal ───────────────────────────────────────────
    if subscription.status in (SubscriptionStatus.active, SubscriptionStatus.expires_soon):
        # Do not mutate tariff/expiry until VPN access is proven. Paid-payment
        # recovery may retry this function; extending first would duplicate days
        # on every failed fulfillment attempt.
        renewal_key = await subscription_service.ensure_subscription_vpn_key(
            subscription, tariff, session
        )
        if renewal_key is None:
            raise RuntimeError(
                f"Payment {payment.id} renewal has no coherent VPN credential"
            )
        subscription.tariff_id = tariff.id
        await subscription_service.extend_subscription(
            subscription, tariff.duration_days, session
        )
        logger.info(
            "Extended subscription %d by %d days with verified VPN key %d",
            subscription.id, tariff.duration_days, renewal_key.id,
        )
        if telegram_id:
            await notify_payment_confirmed(
                telegram_id,
                float(payment.amount),
                is_renewal=True,
                subscription=subscription,
            )
    else:
        subscription.status = SubscriptionStatus.pending_payment
        activated_key = await subscription_service.activate_subscription(
            subscription, tariff, session, send_key_callback=send_key_to_user,
        )
        if activated_key is None:
            raise RuntimeError(f"Payment {payment.id} VPN activation failed")

    await _process_referral_reward(payment.user_id, session, payment=payment)


_REVSHARE_PERCENT = Decimal("0.20")
_MIN_REVSHARE_AMOUNT = Decimal("0.01")


async def _process_referral_reward(user_id: int, session: AsyncSession, payment: Payment | None = None) -> None:
    result = await session.execute(
        select(Referral).where(Referral.invited_user_id == user_id)
    )
    referral = result.scalar_one_or_none()
    if referral is None:
        return

    # First payment: activate referral, then check milestones.
    # Only for real paid tariffs — skip free/trial activations.
    _skip_activation = True
    if (
        referral.status == ReferralStatus.pending
        and payment is not None
        and payment.amount is not None
        and payment.amount > Decimal("0")
    ):
        _paid_tariff = await session.get(Tariff, payment.tariff_id) if payment.tariff_id else None
        if _paid_tariff is None or _paid_tariff.trial_days is None:
            _skip_activation = False

    if referral.status == ReferralStatus.pending and not _skip_activation:
        referral.status = ReferralStatus.activated
        referral.activated_at = datetime.utcnow()
        await session.flush()

        # Count total activated referrals for this referrer (including the one just activated)
        count_result = await session.execute(
            select(func.count(Referral.id)).where(
                Referral.referrer_id == referral.referrer_id,
                Referral.status == ReferralStatus.activated,
            )
        )
        activated_count = count_result.scalar_one() or 0

        _MILESTONES = {1: 7, 3: 30, 10: 90}
        if activated_count in _MILESTONES:
            bonus_days = _MILESTONES[activated_count]
            referrer_sub_result = await session.execute(
                select(Subscription).where(
                    Subscription.user_id == referral.referrer_id,
                    Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
                ).order_by(Subscription.expires_at.desc())
            )
            ref_sub = referrer_sub_result.scalars().first()
            if ref_sub is not None:
                await subscription_service.extend_subscription(ref_sub, bonus_days, session)
            # Notify referrer about milestone
            referrer_obj = await session.get(User, referral.referrer_id)
            if referrer_obj:
                from app.services.user_notifications import notify_referral_milestone
                await notify_referral_milestone(
                    referrer_obj.telegram_id, activated_count, bonus_days
                )

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
