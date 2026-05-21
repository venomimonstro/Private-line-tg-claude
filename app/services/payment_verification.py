from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import PaymentStatus

logger = logging.getLogger(__name__)

_MIN_AGE_MINUTES = 15
_MAX_AGE_HOURS = 24


async def verify_pending_payments(session: AsyncSession) -> None:
    """Poll payment providers for pending payments older than 15 min."""
    from app.models.payment import Payment

    now = datetime.utcnow()
    min_created = now - timedelta(hours=_MAX_AGE_HOURS)
    max_created = now - timedelta(minutes=_MIN_AGE_MINUTES)

    result = await session.execute(
        select(Payment).where(
            Payment.status == PaymentStatus.pending,
            Payment.created_at >= min_created,
            Payment.created_at <= max_created,
        )
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
            else:
                # Other providers use webhooks only
                continue

            if not confirmed:
                # Check if the payment is too old and mark as failed
                if payment.created_at < now - timedelta(hours=_MAX_AGE_HOURS - 1):
                    payment.status = PaymentStatus.failed
                    payment.updated_at = now
                    logger.info("Marked payment %d as failed (timeout)", payment.id)
        except Exception as exc:
            logger.error("Error verifying payment %d: %s", payment.id, exc)

    await session.commit()


async def check_payment_status_yookassa(payment: object, session: AsyncSession) -> bool:
    """Returns True if payment was confirmed."""
    import httpx
    from app.config import settings
    from app.payments.services import confirm_payment_auto

    p = payment  # type: ignore[assignment]
    if not p.provider_payment_id:
        return False

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.yookassa.ru/v3/payments/{p.provider_payment_id}",
                auth=(settings.YOOKASSA_SHOP_ID, settings.YOOKASSA_SECRET_KEY),
            )
            resp.raise_for_status()
            data = resp.json()

        status = data.get("status")
        if status == "succeeded":
            logger.info("YooKassa payment %s succeeded, confirming payment %d", p.provider_payment_id, p.id)
            await confirm_payment_auto(p, session)
            return True
        elif status in ("canceled", "cancelled"):
            from datetime import datetime as dt
            p.status = PaymentStatus.failed
            p.updated_at = dt.utcnow()
            logger.info("YooKassa payment %s cancelled, marking payment %d as failed", p.provider_payment_id, p.id)
        return False
    except Exception as exc:
        logger.error("YooKassa status check failed for payment %d: %s", p.id, exc)
        return False


async def check_payment_status_lava(payment: object, session: AsyncSession) -> bool:
    """Returns True if payment was confirmed."""
    import hashlib
    import hmac
    import json

    import httpx
    from app.config import settings
    from app.payments.services import confirm_payment_auto

    p = payment  # type: ignore[assignment]
    if not p.provider_payment_id:
        return False

    try:
        payload = {
            "shopId": settings.LAVA_PROJECT_ID,
            "invoiceId": p.provider_payment_id,
        }
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        sign = hmac.new(
            settings.LAVA_SECRET_KEY.encode(),
            body.encode(),
            hashlib.sha256,
        ).hexdigest()

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.lava.ru/business/invoice/status",
                content=body.encode(),
                headers={"Signature": sign, "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        status = data.get("data", {}).get("status")
        if status == "success":
            logger.info("LAVA payment %s succeeded, confirming payment %d", p.provider_payment_id, p.id)
            await confirm_payment_auto(p, session)
            return True
        elif status in ("fail", "expired"):
            from datetime import datetime as dt
            p.status = PaymentStatus.failed
            p.updated_at = dt.utcnow()
            logger.info("LAVA payment %s failed/expired, marking payment %d as failed", p.provider_payment_id, p.id)
        return False
    except Exception as exc:
        logger.error("LAVA status check failed for payment %d: %s", p.id, exc)
        return False
