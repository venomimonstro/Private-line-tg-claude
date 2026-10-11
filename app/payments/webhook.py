from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_session
from app.enums import PaymentStatus
from app.models.payment import Payment
from app.payments.registry import get_provider_db
from app.payments.services import confirm_payment_auto

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)

# Providers that expect a plain-text "YES" response (not JSON)
_PLAINTEXT_YES_PROVIDERS = {"freekassa"}

_SUCCESS_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Оплата прошла</title>
<style>body{font-family:sans-serif;text-align:center;padding:60px 20px;background:#f8fff8}
h1{color:#28a745}p{color:#555}a{color:#007bff}</style></head>
<body><h1>✅ Оплата прошла успешно!</h1>
<p>Вернитесь в Telegram-бот — ваш доступ будет активирован автоматически.</p>
<p><a href="https://t.me/">Открыть Telegram</a></p></body></html>"""

_FAILED_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Оплата не завершена</title>
<style>body{font-family:sans-serif;text-align:center;padding:60px 20px;background:#fff8f8}
h1{color:#dc3545}p{color:#555}a{color:#007bff}</style></head>
<body><h1>❌ Оплата не завершена</h1>
<p>Попробуйте снова или свяжитесь с поддержкой через Telegram-бот.</p>
<p><a href="https://t.me/">Открыть Telegram</a></p></body></html>"""


@router.post("/payment/{provider_name}", response_model=None)
async def payment_webhook(
    provider_name: str,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> PlainTextResponse | dict:
    try:
        provider = await get_provider_db(provider_name, session)
    except ValueError:
        raise HTTPException(status_code=404, detail="Unknown provider")

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    # YooMoney's "test notification" is a connectivity check, not a payment.
    # Their UI can send an empty test request; acknowledge only this explicit
    # non-payment case without requiring a payment signature.
    yoomoney_test = False
    if provider_name == "yoomoney":
        try:
            ym = parse_qs(
                body.decode("utf-8", errors="strict"),
                keep_blank_values=True,
            )
            yoomoney_test = (
                not body
                or (ym.get("test_notification", ["false"])[0].lower() == "true")
            )
        except Exception:
            yoomoney_test = False

    try:
        incoming = await provider.verify_webhook(body, headers)
    finally:
        await provider.aclose()
    if incoming is None:
        if provider_name == "yoomoney" and yoomoney_test:
            return {"status": "ok", "detail": "test_notification"}
        logger.warning("Webhook signature verification failed for provider %s", provider_name)
        # Never ACK an invalid real notification. FreeKassa retries until it
        # receives the literal YES; YooMoney/YooKassa retry non-success delivery.
        if provider_name in _PLAINTEXT_YES_PROVIDERS:
            return PlainTextResponse("INVALID", status_code=403)
        raise HTTPException(status_code=403, detail="Invalid signature")

    result = await session.get(Payment, incoming.our_payment_id)
    if result is None:
        logger.error("Payment %d not found (provider: %s)", incoming.our_payment_id, provider_name)
        # This can be a commit race: the provider can deliver faster than our
        # checkout transaction becomes visible. Do not acknowledge it as
        # processed; ask the provider to retry.
        if provider_name in _PLAINTEXT_YES_PROVIDERS:
            return PlainTextResponse("RETRY", status_code=503)
        raise HTTPException(status_code=503, detail="Payment record not ready")

    logger.info(
        "Webhook payment lookup: id=%d provider=%s status=%s amount=%s",
        result.id, provider_name, result.status, result.amount,
    )

    # Signature validity alone does not authorize a different order/provider
    # or amount. Validate the signed provider payload against our immutable
    # payment record before any state transition.
    try:
        incoming_amount = Decimal(str(incoming.amount)).quantize(Decimal("0.01"))
        expected_amount = Decimal(str(result.amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        logger.error("Webhook payment %d has invalid monetary amount", result.id)
        raise HTTPException(status_code=400, detail="Invalid amount")

    # Provider adapters normalize IncomingPayment.amount to the customer/order
    # amount. Keep the shared authorization invariant exact; never allow a
    # provider-specific underpayment tolerance here.
    amount_ok = incoming_amount == expected_amount
    provider_ok = result.provider == provider_name
    currency_ok = (incoming.currency or "").upper() == (result.currency or "").upper() == "RUB"
    if not (provider_ok and currency_ok and amount_ok):
        logger.error(
            "Webhook invariant mismatch payment=%d provider_ok=%s currency_ok=%s amount_ok=%s",
            result.id, provider_ok, currency_ok, amount_ok,
        )
        if provider_name in _PLAINTEXT_YES_PROVIDERS:
            return PlainTextResponse("MISMATCH", status_code=409)
        raise HTTPException(status_code=409, detail="Payment does not match order")

    if result.status == PaymentStatus.paid:
        # A provider retry is also a recovery signal for the durable state
        # PAID + fulfilled_at=NULL. confirm_payment_auto is row-lock/idempotent.
        if result.fulfilled_at is None:
            await confirm_payment_auto(result, session)
            state = "recovery_processed"
        else:
            state = "already_processed"
        if provider_name in _PLAINTEXT_YES_PROVIDERS:
            return PlainTextResponse("YES")
        return {"status": state}

    if result.status != PaymentStatus.pending:
        logger.info(
            "Payment %d already in terminal/non-payable status %s",
            result.id, result.status,
        )
        if provider_name in _PLAINTEXT_YES_PROVIDERS:
            return PlainTextResponse("YES")
        return {"status": "already_processed"}

    result.provider_payment_id = incoming.provider_payment_id
    await confirm_payment_auto(result, session)

    if provider_name in _PLAINTEXT_YES_PROVIDERS:
        return PlainTextResponse("YES")
    return {"status": "ok"}


@router.get("/payment/freekassa")
async def freekassa_url_check() -> PlainTextResponse:
    """FreeKassa validates the notification URL with a GET request expecting 'YES'."""
    return PlainTextResponse("YES")


@router.get("/payment/success", response_class=HTMLResponse)
async def payment_success() -> HTMLResponse:
    return HTMLResponse(_SUCCESS_HTML)


@router.get("/payment/failed", response_class=HTMLResponse)
async def payment_failed() -> HTMLResponse:
    return HTMLResponse(_FAILED_HTML)
