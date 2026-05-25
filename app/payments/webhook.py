from __future__ import annotations

import logging

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

    incoming = await provider.verify_webhook(body, headers)
    if incoming is None:
        logger.warning("Webhook signature verification failed for provider %s", provider_name)
        # FreeKassa sends a test POST with fake data to validate the URL.
        # Return YES so their validation passes; no payment is processed.
        if provider_name in _PLAINTEXT_YES_PROVIDERS:
            return PlainTextResponse("YES")
        raise HTTPException(status_code=400, detail="Invalid signature")

    result = await session.get(Payment, incoming.our_payment_id)
    if result is None:
        logger.error("Payment %d not found (provider: %s)", incoming.our_payment_id, provider_name)
        # Return success to prevent infinite retries for unknown payments
        if provider_name in _PLAINTEXT_YES_PROVIDERS:
            return PlainTextResponse("YES")
        return {"status": "ok"}

    if result.status != PaymentStatus.pending:
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
