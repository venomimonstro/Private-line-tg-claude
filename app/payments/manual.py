from __future__ import annotations

from app.config import settings
from app.payments.provider_base import IncomingPayment, PaymentProviderBase, PaymentResult


class ManualPaymentProvider(PaymentProviderBase):
    """Manual payment provider — admin confirms payments by hand."""

    async def create_payment(
        self,
        our_payment_id: int,
        amount: float,
        currency: str,
        description: str,
        user_id: int,
    ) -> PaymentResult:
        return PaymentResult(
            provider_payment_id="manual",
            payment_url=None,
            display_data={"instructions": settings.PAYMENT_INSTRUCTIONS},
        )

    async def verify_webhook(
        self,
        body: bytes,
        headers: dict[str, str],
    ) -> IncomingPayment | None:
        return None  # Manual only via admin panel

    def get_instructions(self) -> str:
        return settings.PAYMENT_INSTRUCTIONS
