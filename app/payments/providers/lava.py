from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from app.payments.provider_base import IncomingPayment, PaymentProviderBase, PaymentResult


class LavaProvider(PaymentProviderBase):
    """LAVA.ru payment API.

    Config: LAVA_PROJECT_ID, LAVA_SECRET_KEY
    """

    _API_URL = "https://api.lava.ru/business/invoice/create"

    def __init__(self, project_id: str, secret_key: str, webhook_secret: str) -> None:
        self._project_id = project_id.strip()
        self._secret = secret_key.strip()
        self._webhook_secret = webhook_secret.strip()
        self._http = httpx.AsyncClient(timeout=15.0, trust_env=False)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _sign(self, data: dict) -> str:
        body = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hmac.new(self._secret.encode(), body.encode(), hashlib.sha256).hexdigest()

    async def create_payment(
        self,
        our_payment_id: int,
        amount: float,
        currency: str,
        description: str,
        user_id: int,
    ) -> PaymentResult:
        payload = {
            "comment": description,
            "orderId": str(our_payment_id),
            "shopId": self._project_id,
            "sum": round(amount, 2),
        }
        sign = self._sign(payload)
        resp = await self._http.post(
            self._API_URL,
            json=payload,
            headers={"Signature": sign, "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

        inv_id = data["data"]["id"]
        inv_url = data["data"]["url"]
        return PaymentResult(provider_payment_id=inv_id, payment_url=inv_url)

    async def get_invoice_status_by_order_id(self, order_id: str) -> dict:
        """Query the exact merchant order using the API-signing secret."""
        payload = {
            "shopId": self._project_id,
            "orderId": str(order_id),
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        signature = hmac.new(
            self._secret.encode(), body.encode(), hashlib.sha256
        ).hexdigest()
        response = await self._http.post(
            "https://api.lava.ru/business/invoice/status",
            content=body.encode(),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Signature": signature,
            },
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status_check") is not True or not isinstance(data.get("data"), dict):
            raise RuntimeError("LAVA invoice status response is not successful")
        return data["data"]

    async def verify_webhook(
        self,
        body: bytes,
        headers: dict[str, str],
    ) -> IncomingPayment | None:
        try:
            sig = headers.get("signature") or headers.get("x-api-signature") or ""
            expected = hmac.new(
                self._webhook_secret.encode(), body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, sig):
                return None
            data = json.loads(body)
            if data.get("status") != "success":
                return None
            return IncomingPayment(
                our_payment_id=int(data["orderId"]),
                provider_payment_id=data.get("invoiceId", data["orderId"]),
                amount=float(data.get("amount", 0)),
                currency="RUB",
            )
        except Exception:
            return None
