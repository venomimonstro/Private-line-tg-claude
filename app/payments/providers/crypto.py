from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from app.payments.provider_base import IncomingPayment, PaymentProviderBase, PaymentResult


class CryptoBotProvider(PaymentProviderBase):
    """CryptoBot (t.me/CryptoBot) payment API.

    Config: CRYPTO_BOT_TOKEN
    """

    _API_URL = "https://pay.crypt.bot/api"

    def __init__(self, token: str) -> None:
        self._token = token
        self._http = httpx.AsyncClient(timeout=15.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _sign(self, body: bytes) -> str:
        return hmac.new(self._token.encode(), body, hashlib.sha256).hexdigest()

    async def create_payment(
        self,
        our_payment_id: int,
        amount: float,
        currency: str,
        description: str,
        user_id: int,
    ) -> PaymentResult:
        payload_str = f"pl_{our_payment_id}"
        body = {
            "currency_type": "fiat",
            "fiat": "RUB",
            "amount": str(int(amount)),
            "description": description,
            "payload": payload_str,
            "allow_comments": False,
            "allow_anonymous": False,
        }
        resp = await self._http.post(
            f"{self._API_URL}/createInvoice",
            json=body,
            headers={"Crypto-Pay-API-Token": self._token},
        )
        resp.raise_for_status()
        data = resp.json()

        if not data.get("ok"):
            raise RuntimeError(f"CryptoBot error: {data}")

        result = data["result"]
        return PaymentResult(
            provider_payment_id=str(result["invoice_id"]),
            payment_url=result["pay_url"],
        )

    async def verify_webhook(
        self,
        body: bytes,
        headers: dict[str, str],
    ) -> IncomingPayment | None:
        try:
            sig = (
                headers.get("crypto-pay-api-signature")
                or headers.get("Crypto-Pay-Api-Signature", "")
            )
            expected = self._sign(body)
            if not hmac.compare_digest(expected, sig):
                return None

            data = json.loads(body)
            if data.get("update_type") != "invoice_paid":
                return None

            p = data["payload"]
            payload_str = p.get("payload", "")
            if not payload_str.startswith("pl_"):
                return None
            our_id = int(payload_str[3:])
            return IncomingPayment(
                our_payment_id=our_id,
                provider_payment_id=str(p["invoice_id"]),
                amount=float(p.get("amount", 0)),
                currency=p.get("asset", ""),
            )
        except Exception:
            return None
