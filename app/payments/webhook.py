from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_session
from app.enums import PaymentStatus
from app.models.payment import Payment
from app.payments.registry import get_provider
from app.payments.services import confirm_payment_auto

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


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
