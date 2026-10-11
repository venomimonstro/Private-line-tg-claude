from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import PaymentStatus

logger = logging.getLogger(__name__)

_MIN_AGE_MINUTES = 15
_MAX_AGE_HOURS = 168
_BATCH_LIMIT = 100


async def verify_pending_payments(session: AsyncSession) -> None:
    """Poll recent pending payments; only explicit provider terminal states may fail them."""
    from app.models.payment import Payment

    now = datetime.utcnow()
    min_created = now - timedelta(hours=_MAX_AGE_HOURS)
    max_created = now - timedelta(minutes=_MIN_AGE_MINUTES)

    result = await session.execute(
        select(Payment)
        .where(
            Payment.status == PaymentStatus.pending,
            Payment.created_at >= min_created,
            Payment.created_at <= max_created,
        )
        .order_by(Payment.created_at)
        .limit(_BATCH_LIMIT)
    )
    payments = result.scalars().all()

    if not payments:
        logger.debug("No pending payments to verify")
        return

    logger.info("Verifying %d pending payments", len(payments))
    for payment in payments:
        try:
            if payment.provider == "yookassa":
                confirmed = await check_payment_status_yookassa(payment, session)
            elif payment.provider == "lava":
                confirmed = await check_payment_status_lava(payment, session)
            elif payment.provider == "yoomoney":
                confirmed = await check_payment_status_yoomoney(payment, session)
            else:
                continue

            # Never infer a failed payment from age or a polling error.
            # Provider-specific checks mark only explicit terminal states
            # (canceled/fail/expired). Webhooks may arrive late and provider APIs
            # can be transiently unavailable.
            if not confirmed:
                logger.debug("Payment %d remains pending", payment.id)
        except Exception as exc:
            logger.error("Error verifying payment %d: %s", payment.id, exc)

    await session.commit()


async def check_payment_status_yoomoney(
    payment: object,
    session: AsyncSession,
) -> bool:
    """Do not fulfill YooMoney orders from operation-history alone.

    For incoming QuickPay transfers the history amount is the wallet settlement
    after commission, while the signed HTTP notification carries
    withdraw_amount (the sender debit/order amount). A percentage tolerance on
    history data would permit underpayment. Signed webhooks are authoritative;
    polling is retained only to surface a candidate operation in logs.
    """
    from app.payments.registry import get_provider_db

    p = payment  # type: ignore[assignment]
    label = p.provider_payment_id
    if not label:
        return False
    provider = None
    try:
        provider = await get_provider_db("yoomoney", session)
        op_id = await provider.check_payment_by_label(label, float(p.amount))  # type: ignore[attr-defined]
        if op_id:
            logger.warning(
                "YooMoney candidate found for payment %d (label=%s op=%s), "
                "but auto-fulfillment requires the signed webhook",
                p.id, label, op_id,
            )
    except Exception as exc:
        logger.error("YooMoney status check failed for payment %d: %s", p.id, exc)
    finally:
        if provider is not None:
            await provider.aclose()
    return False


async def check_payment_status_yookassa(
    payment: object,
    session: AsyncSession,
    client: httpx.AsyncClient | None = None,
) -> bool:
    from app.payments.providers.yookassa import YooKassaProvider
    from app.payments.registry import get_provider_db
    from app.payments.services import confirm_payment_auto

    p = payment  # type: ignore[assignment]
    if not p.provider_payment_id:
        return False

    provider = await get_provider_db("yookassa", session)
    if not isinstance(provider, YooKassaProvider):
        await provider.aclose()
        raise RuntimeError("YooKassa provider registry returned unexpected type")
    try:
        data = await provider.get_payment(str(p.provider_payment_id))
        status = data.get("status")

        if status == "succeeded":
            metadata = data.get("metadata") or {}
            amount = data.get("amount") or {}
            try:
                verified_order = int(metadata.get("order_id", 0))
                verified_amount = Decimal(str(amount.get("value"))).quantize(
                    Decimal("0.01")
                )
                expected_amount = Decimal(str(p.amount)).quantize(Decimal("0.01"))
            except (TypeError, ValueError, InvalidOperation):
                logger.error("YooKassa payment %d returned invalid identity/amount", p.id)
                return False

            if (
                data.get("paid") is not True
                or verified_order != p.id
                or amount.get("currency") != "RUB"
                or verified_amount != expected_amount
            ):
                logger.error(
                    "YooKassa polling invariant mismatch payment=%d "
                    "paid=%s order_ok=%s currency_ok=%s amount_ok=%s",
                    p.id,
                    data.get("paid") is True,
                    verified_order == p.id,
                    amount.get("currency") == "RUB",
                    verified_amount == expected_amount,
                )
                return False

            logger.info(
                "YooKassa payment %s verified by API for local payment %d",
                p.provider_payment_id, p.id,
            )
            await confirm_payment_auto(p, session)
            return True

        if status in ("canceled", "cancelled"):
            p.status = PaymentStatus.failed
            p.updated_at = datetime.utcnow()
            logger.info(
                "YooKassa payment %s cancelled, marking payment %d as failed",
                p.provider_payment_id, p.id,
            )
        return False
    except Exception as exc:
        logger.error("YooKassa status check failed for payment %d: %s", p.id, exc)
        return False
    finally:
        await provider.aclose()


async def check_payment_status_lava(
    payment: object,
    session: AsyncSession,
    client: httpx.AsyncClient | None = None,
) -> bool:
    from app.payments.providers.lava import LavaProvider
    from app.payments.registry import get_provider_db
    from app.payments.services import confirm_payment_auto

    p = payment  # type: ignore[assignment]
    if not p.provider_payment_id:
        return False

    provider = await get_provider_db("lava", session)
    if not isinstance(provider, LavaProvider):
        await provider.aclose()
        raise RuntimeError("LAVA provider registry returned unexpected type")
    try:
        # LAVA explicitly supports invoice/status by merchant orderId. Querying
        # with our immutable local payment id cryptographically binds the
        # provider response to this order instead of trusting a stored invoiceId.
        data = await provider.get_invoice_status_by_order_id(str(p.id))
        status = data.get("status")

        if status == "success":
            returned_order = data.get("orderId") or data.get("order_id")
            returned_invoice = data.get("id") or data.get("invoiceId")
            returned_sum = data.get("sum") or data.get("amount")

            if returned_order is not None and str(returned_order) != str(p.id):
                logger.error("LAVA order identity mismatch for payment %d", p.id)
                return False
            if (
                returned_invoice is not None
                and str(returned_invoice) != str(p.provider_payment_id)
            ):
                logger.error("LAVA invoice identity mismatch for payment %d", p.id)
                return False
            if returned_sum is not None:
                try:
                    actual = Decimal(str(returned_sum)).quantize(Decimal("0.01"))
                    expected = Decimal(str(p.amount)).quantize(Decimal("0.01"))
                except InvalidOperation:
                    return False
                if actual != expected:
                    logger.error("LAVA amount mismatch for payment %d", p.id)
                    return False

            logger.info(
                "LAVA order %d verified by signed status API; confirming payment",
                p.id,
            )
            await confirm_payment_auto(p, session)
            return True

        if status in ("fail", "expired"):
            p.status = PaymentStatus.failed
            p.updated_at = datetime.utcnow()
            logger.info(
                "LAVA order %d failed/expired, marking local payment failed", p.id
            )
        return False
    except Exception as exc:
        logger.error("LAVA status check failed for payment %d: %s", p.id, exc)
        return False
    finally:
        await provider.aclose()
