from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qs, quote

from app.payments.provider_base import IncomingPayment, PaymentProviderBase, PaymentResult


class RobokassaProvider(PaymentProviderBase):
    """Robokassa payment.

    Config: ROBOKASSA_MERCHANT_LOGIN, ROBOKASSA_PASSWORD_1, ROBOKASSA_PASSWORD_2
    """

    _CHECKOUT_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"

    def __init__(self, merchant_login: str, password_1: str, password_2: str) -> None:
        self._login = merchant_login
        self._pwd1 = password_1
        self._pwd2 = password_2

    def _sign_request(self, amount: float, inv_id: int) -> str:
        raw = f"{self._login}:{amount:.2f}:{inv_id}:{self._pwd1}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _sign_notify(self, amount: str, inv_id: str) -> str:
        raw = f"{amount}:{inv_id}:{self._pwd2}"
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
            f"?MrchLogin={self._login}"
            f"&OutSum={amount:.2f}"
            f"&InvId={our_payment_id}"
            f"&Description={quote(description)}"
            f"&SignatureValue={sign}"
            f"&Encoding=utf-8"
        )
        return PaymentResult(provider_payment_id=str(our_payment_id), payment_url=url)

    async def verify_webhook(
        self,
        body: bytes,
        headers: dict[str, str],
    ) -> IncomingPayment | None:
        try:
            params = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            amount = params.get("OutSum", "")
            inv_id = params.get("InvId", "")
            sig = params.get("SignatureValue", "").lower()
            expected = self._sign_notify(amount, inv_id)
            if not hmac.compare_digest(expected, sig):
                return None
            return IncomingPayment(
                our_payment_id=int(inv_id),
                provider_payment_id=inv_id,
                amount=float(amount),
                currency="RUB",
            )
        except Exception:
            return None
