from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qs

from app.payments.provider_base import IncomingPayment, PaymentProviderBase, PaymentResult


class FreeKassaProvider(PaymentProviderBase):
    """FreeKassa classic URL-based integration.

    Config: FREEKASSA_MERCHANT_ID, FREEKASSA_SECRET_1, FREEKASSA_SECRET_2
    """

    _CHECKOUT_URL = "https://pay.freekassa.ru/"

    def __init__(self, merchant_id: str, secret_1: str, secret_2: str) -> None:
        self._merchant_id = merchant_id
        self._secret_1 = secret_1
        self._secret_2 = secret_2

    def _sign_request(self, amount: float, order_id: int) -> str:
        raw = f"{self._merchant_id}:{amount:.2f}:{self._secret_1}:RUB:{order_id}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _sign_notification(self, amount: str, order_id: str) -> str:
        raw = f"{self._merchant_id}:{amount}:{self._secret_2}:{order_id}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    async def create_payment(
        self,
        our_payment_id: int,
        amount: float,
        currency: str,
        description: str,
        user_id: int,
    ) -> PaymentResult:
        sign = self._sign_request(amount, our_payment_id)
        url = (
            f"{self._CHECKOUT_URL}"
            f"?m={self._merchant_id}"
            f"&oa={amount:.2f}"
            f"&currency=RUB"
            f"&o={our_payment_id}"
            f"&s={sign}"
            f"&lang=ru"
        )
        return PaymentResult(
            provider_payment_id=str(our_payment_id),
            payment_url=url,
        )

    async def verify_webhook(
        self,
        body: bytes,
        headers: dict[str, str],
    ) -> IncomingPayment | None:
        try:
            params = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            amount = params.get("AMOUNT", "")
            order_id = params.get("MERCHANT_ORDER_ID", "")
            sign = params.get("SIGN", "").lower()
            expected = self._sign_notification(amount, order_id)
            if not hmac.compare_digest(expected, sign):
                return None
            return IncomingPayment(
                our_payment_id=int(order_id),
                provider_payment_id=params.get("intid", order_id),
                amount=float(amount),
                currency="RUB",
            )
        except Exception:
            return None
