from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_session
from app.enums import PaymentStatus
from app.models.payment import Payment
from app.payments.registry import get_provider
from app.payments.services import confirm_payment_auto

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)

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


@router.post("/payment/{provider_name}")
async def payment_webhook(
    provider_name: str,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> dict:
    try:
        provider = get_provider(provider_name)
    except ValueError:
        raise HTTPException(status_code=404, detail="Unknown provider")

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    incoming = await provider.verify_webhook(body, headers)
    if incoming is None:
        logger.warning("Webhook signature verification failed for provider %s", provider_name)
        raise HTTPException(status_code=400, detail="Invalid signature")

    result = await session.get(Payment, incoming.our_payment_id)
    if result is None:
        logger.error("Payment %d not found (provider: %s)", incoming.our_payment_id, provider_name)
        return {"status": "ok"}  # Return 200 to prevent retries for missing payments

    if result.status != PaymentStatus.pending:
        return {"status": "already_processed"}

    result.provider_payment_id = incoming.provider_payment_id
    await confirm_payment_auto(result, session)

    return {"status": "ok"}


@router.get("/payment/success", response_class=HTMLResponse)
async def payment_success() -> HTMLResponse:
    return HTMLResponse(_SUCCESS_HTML)


@router.get("/payment/failed", response_class=HTMLResponse)
async def payment_failed() -> HTMLResponse:
    return HTMLResponse(_FAILED_HTML)
