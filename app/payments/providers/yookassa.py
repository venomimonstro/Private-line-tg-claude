from __future__ import annotations

import json
import uuid

import httpx

from app.payments.provider_base import IncomingPayment, PaymentProviderBase, PaymentResult


class YooKassaProvider(PaymentProviderBase):
    """YooKassa (ЮKassa) payment API.

    Config: YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY
    """

    _API_URL = "https://api.yookassa.ru/v3"

    def __init__(self, shop_id: str, secret_key: str) -> None:
        self._shop_id = shop_id
        self._secret = secret_key
        self._http = httpx.AsyncClient(timeout=15.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def create_payment(
        self,
        our_payment_id: int,
        amount: float,
        currency: str,
        description: str,
        user_id: int,
    ) -> PaymentResult:
        from app.config import settings

        payload = {
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "description": description,
            "confirmation": {
                "type": "redirect",
                "return_url": f"https://t.me/{settings.BOT_USERNAME}",
            },
            "metadata": {"order_id": str(our_payment_id)},
            "capture": True,
        }
        # Stable for one local payment row: if the request reached YooKassa
        # but the HTTP response was lost, the retry returns the same payment
        # instead of creating a duplicate checkout.
        idempotence_key = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"private-line:yookassa:{self._shop_id}:{our_payment_id}",
            )
        )
        request_kwargs = {
            "json": payload,
            "auth": (self._shop_id, self._secret),
            "headers": {
                "Idempotence-Key": idempotence_key,
                "Content-Type": "application/json",
            },
        }
        try:
            resp = await self._http.post(
                f"{self._API_URL}/payments",
                **request_kwargs,
            )
        except httpx.TransportError:
            # One ambiguity-safe retry with exactly the same idempotence key.
            resp = await self._http.post(
                f"{self._API_URL}/payments",
                **request_kwargs,
            )
        resp.raise_for_status()
        data = resp.json()

        pay_id = data["id"]
        confirm_url = data["confirmation"]["confirmation_url"]
        return PaymentResult(provider_payment_id=pay_id, payment_url=confirm_url)

    async def verify_webhook(
        self,
        body: bytes,
        headers: dict[str, str],
    ) -> IncomingPayment | None:
        try:
            data = json.loads(body)
            if data.get("event") != "payment.succeeded":
                return None
            obj = data["object"]
            pay_id = obj["id"]

            # YooKassa recommends verifying notification authenticity by
            # requesting the object from its API. Treat the webhook body only
            # as a trigger/payment-id carrier; all authorization fields below
            # come from the authenticated API response.
            resp2 = await self._http.get(
                f"{self._API_URL}/payments/{pay_id}",
                auth=(self._shop_id, self._secret),
            )
            resp2.raise_for_status()
            verified = resp2.json()
            if (
                verified.get("id") != pay_id
                or verified.get("status") != "succeeded"
                or verified.get("paid") is not True
            ):
                return None

            metadata = verified.get("metadata") or {}
            try:
                order_id = int(metadata.get("order_id", 0))
            except (TypeError, ValueError):
                return None
            if order_id <= 0:
                return None

            amount_obj = verified.get("amount") or {}
            if amount_obj.get("currency") != "RUB":
                return None
            amount_val = float(amount_obj["value"])

            return IncomingPayment(
                our_payment_id=order_id,
                provider_payment_id=pay_id,
                amount=amount_val,
                currency="RUB",
            )
        except Exception:
            return None
