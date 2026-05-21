from __future__ import annotations

from app.payments.provider_base import IncomingPayment, PaymentProviderBase, PaymentResult


class TelegramStarsProvider(PaymentProviderBase):
    """Telegram Stars payment provider.

    Payment is initiated by the bot (send_invoice with XTR currency).
    Webhook verification is not used — payment is confirmed via the
    successful_payment handler in bot/handlers/stars_payment.py.
    """

    async def create_payment(
        self,
        our_payment_id: int,
        amount: float,
        currency: str,
        description: str,
        user_id: int,
    ) -> PaymentResult:
        # No URL — bot will call send_invoice
        return PaymentResult(
            provider_payment_id=f"stars_{our_payment_id}",
            payment_url=None,
            display_data={"stars_flow": True, "our_payment_id": our_payment_id},
        )

    async def verify_webhook(
        self,
        body: bytes,
        headers: dict[str, str],
    ) -> IncomingPayment | None:
        # Stars are confirmed via aiogram successful_payment handler, not webhook
        return None
