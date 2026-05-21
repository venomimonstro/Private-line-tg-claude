from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlencode

from app.payments.provider_base import IncomingPayment, PaymentProviderBase, PaymentResult


class ProdamusProvider(PaymentProviderBase):
    """Prodamus payment provider.

    Config: PRODAMUS_API_URL, PRODAMUS_WEBHOOK_SECRET
    """

    def __init__(self, api_url: str, webhook_secret: str) -> None:
        self._api_url = api_url.rstrip("/")
        self._secret = webhook_secret

    def _sign(self, data: dict) -> str:
        sorted_str = "&".join(f"{k}={v}" for k, v in sorted(data.items()))
        return hmac.new(self._secret.encode(), sorted_str.encode(), hashlib.sha256).hexdigest()

    async def create_payment(
        self,
        our_payment_id: int,
        amount: float,
        currency: str,
        description: str,
        user_id: int,
    ) -> PaymentResult:
        params: dict[str, str] = {
            "order_id": str(our_payment_id),
            "amount": f"{amount:.2f}",
            "currency": "RUB",
            "description": description,
        }
        params["signature"] = self._sign(params)
        url = f"{self._api_url}?{urlencode(params)}"
        return PaymentResult(provider_payment_id=str(our_payment_id), payment_url=url)

    async def verify_webhook(
        self,
        body: bytes,
        headers: dict[str, str],
    ) -> IncomingPayment | None:
        try:
            # Try JSON first, then form data
            try:
                data: dict = json.loads(body)
            except Exception:
                data = {k: v[0] for k, v in parse_qs(body.decode()).items()}

            received_sig = data.pop("signature", "")
            expected = self._sign(data)
            if not hmac.compare_digest(expected, received_sig):
                return None

            order_id = data.get("order_id", "")
            amount = float(data.get("amount", 0))
            return IncomingPayment(
                our_payment_id=int(order_id),
                provider_payment_id=order_id,
                amount=amount,
                currency="RUB",
            )
        except Exception:
            return None
