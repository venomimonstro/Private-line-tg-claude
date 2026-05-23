from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import parse_qs

import httpx

from app.payments.provider_base import IncomingPayment, PaymentProviderBase, PaymentResult


class FreeKassaProvider(PaymentProviderBase):
    """FreeKassa API v2 integration.

    Required config keys: FREEKASSA_MERCHANT_ID, FREEKASSA_SECRET_2, FREEKASSA_API_KEY
    Docs: https://docs.freekassa.net/
    """

    _API_URL = "https://api.freekassa.net/v1"

    def __init__(
        self,
        merchant_id: str,
        secret_2: str,
        api_key: str,
        secret_1: str = "",  # kept for backward compat, not used in v2
    ) -> None:
        self._merchant_id = merchant_id
        self._secret_2 = secret_2
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=15.0)

    def _sign_order(self, nonce: int) -> str:
        msg = f"{self._merchant_id}|{nonce}".encode("utf-8")
        return hmac.new(self._api_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()

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
        nonce = int(time.time())
        payload = {
            "shopId": int(self._merchant_id),
            "nonce": nonce,
            "paymentId": str(our_payment_id),
            "i": 0,
            "email": f"user{user_id}@order.local",
            "ip": "127.0.0.1",
            "amount": round(amount, 2),
            "currency": "RUB",
            "signature": self._sign_order(nonce),
        }
        resp = await self._client.post(f"{self._API_URL}/orders/create", json=payload)
        resp.raise_for_status()
        data = resp.json()
        location: str = data.get("location", "") or data.get("orderUrl", "")
        return PaymentResult(
            provider_payment_id=str(data.get("orderId", our_payment_id)),
            payment_url=location or None,
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

    async def aclose(self) -> None:
        await self._client.aclose()
