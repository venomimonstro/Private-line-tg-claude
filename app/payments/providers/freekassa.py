from __future__ import annotations

import hashlib
import hmac
import logging
import urllib.parse
from urllib.parse import parse_qs

from app.payments.provider_base import IncomingPayment, PaymentProviderBase, PaymentResult

logger = logging.getLogger(__name__)


class FreeKassaProvider(PaymentProviderBase):
    """FreeKassa integration using pay.freekassa.ru payment form.

    Signature (form):         MD5(merchant_id:amount:secret1:order_id)
    Signature (notification): MD5(merchant_id:amount:secret2:order_id)
    Response expected:        "YES" plain text

    Required: FREEKASSA_MERCHANT_ID, FREEKASSA_SECRET_1, FREEKASSA_SECRET_2
    Optional: FREEKASSA_CURRENCY_ID — payment system / currency ID (parameter i).
              Leave blank to let user choose. Common values:
              643 = RUB card, 1 = Visa/MC, 7 = QIWI — see FreeKassa LK for your list.
    """

    def __init__(
        self,
        merchant_id: str,
        secret_1: str,
        secret_2: str,
        currency_id: str = "",
    ) -> None:
        self._merchant_id = merchant_id
        self._secret_1 = secret_1
        self._secret_2 = secret_2
        self._currency_id = currency_id

    def _sign_form(self, amount_str: str, order_id: str) -> str:
        raw = f"{self._merchant_id}:{amount_str}:{self._secret_1}:{order_id}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _sign_notification(self, amount: str, order_id: str) -> str:
        raw = f"{self._merchant_id}:{amount}:{self._secret_2}:{order_id}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _fmt_amount(amount: float) -> str:
        """Format amount: no trailing zeros for whole numbers (100 not 100.00)."""
        if amount == int(amount):
            return str(int(amount))
        return f"{amount:.2f}"

    async def create_payment(
        self,
        our_payment_id: int,
        amount: float,
        currency: str,
        description: str,
        user_id: int,
    ) -> PaymentResult:
        amount_str = self._fmt_amount(amount)
        sign = self._sign_form(amount_str, str(our_payment_id))
        params: dict[str, str] = {
            "m": self._merchant_id,
            "oa": amount_str,
            "o": str(our_payment_id),
            "s": sign,
            "lang": "ru",
        }
        if self._currency_id:
            params["i"] = self._currency_id

        url = "https://pay.freekassa.ru/?" + urllib.parse.urlencode(params)
        logger.info(
            "FreeKassa payment URL for order %d amount %s currency_id=%s",
            our_payment_id, amount_str, self._currency_id or "not set",
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

            if not amount or not order_id or not sign:
                logger.warning("FreeKassa webhook missing fields: %s", list(params.keys()))
                return None

            expected = self._sign_notification(amount, order_id)
            if not hmac.compare_digest(expected, sign):
                logger.error(
                    "FreeKassa signature mismatch: expected=%s got=%s order=%s amount=%s",
                    expected, sign, order_id, amount,
                )
                return None

            logger.info(
                "FreeKassa webhook verified: order=%s amount=%s intid=%s",
                order_id, amount, params.get("intid"),
            )
            return IncomingPayment(
                our_payment_id=int(order_id),
                provider_payment_id=params.get("intid", order_id),
                amount=float(amount),
                currency="RUB",
            )
        except Exception as exc:
            logger.error("FreeKassa webhook parse error: %s", exc)
            return None
