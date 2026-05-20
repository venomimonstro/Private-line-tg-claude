from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/payment/{provider}")
async def payment_webhook(provider: str, request: Request) -> dict:
    """Stub endpoint for future payment provider webhooks.

    When a payment provider is integrated, implement provider-specific
    signature verification and payment confirmation logic here.
    """
    return {"status": "not_implemented"}
