from __future__ import annotations

from app.config import settings
from app.payments.provider_base import PaymentProviderBase


class ManualPaymentProvider(PaymentProviderBase):
    """Manual payment provider — admin confirms payments by hand."""

    async def create_payment(self, amount: float, currency: str, metadata: dict) -> str:
        return "manual"

    async def verify_payment(self, provider_payment_id: str) -> bool:
        return False  # Manual payments are confirmed only by admin action.

    def get_payment_instructions(self) -> str:
        return settings.PAYMENT_INSTRUCTIONS
