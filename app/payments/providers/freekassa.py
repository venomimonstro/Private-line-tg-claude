from __future__ import annotations

import hashlib
import hmac
import logging
import urllib.parse
from urllib.parse import parse_qs

from app.payments.provider_base import IncomingPayment, PaymentProviderBase, PaymentResult

logger = logging.getLogger(__name__)

# Docs: https://docs.freekassa.net/
#
# Form signature   (pay.freekassa.ru):  MD5(merchant_id:amount:secret1:currency:order_id)
# Notify signature (callback from FK):  MD5(merchant_id:amount:secret2:order_id)


class FreeKassaProvider(PaymentProviderBase):
    """FreeKassa integration (new platform, pay.freekassa.ru).

    Required settings:
        FREEKASSA_MERCHANT_ID  – shop ID from FreeKassa LK
        FREEKASSA_SECRET_1     – "Секретное слово 1" (for payment form signature)
        FREEKASSA_SECRET_2     – "Секретное слово 2" (for notification verification)

    Optional:
        FREEKASSA_CURRENCY_ID  – payment method / currency selector (param `i`).
                                  Leave blank to let user choose on FreeKassa page.
    """

    _CURRENCY = "RUB"

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

    # ── signatures ────────────────────────────────────────────────────────────

    def _sign_form(self, amount: str, order_id: str) -> str:
        """MD5(merchant_id:amount:secret1:currency:order_id)"""
        raw = f"{self._merchant_id}:{amount}:{self._secret_1}:{self._CURRENCY}:{order_id}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _sign_notify(self, amount: str, order_id: str) -> str:
        """MD5(merchant_id:amount:secret2:order_id)"""
        raw = f"{self._merchant_id}:{amount}:{self._secret_2}:{order_id}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    # ── payment creation ──────────────────────────────────────────────────────

    async def create_payment(
        self,
        our_payment_id: int,
        amount: float,
        currency: str,
        description: str,
        user_id: int,
    ) -> PaymentResult:
        amount_str = f"{amount:.2f}"
        order_id = str(our_payment_id)
        sign = self._sign_form(amount_str, order_id)

        params: dict[str, str] = {
            "m":        self._merchant_id,
            "oa":       amount_str,
            "currency": self._CURRENCY,
            "o":        order_id,
            "s":        sign,
            "lang":     "ru",
        }
        if self._currency_id:
            params["i"] = self._currency_id

        url = "https://pay.freekassa.ru/?" + urllib.parse.urlencode(params)
        logger.info(
            "FreeKassa form URL: merchant=%s amount=%s currency=%s order=%s sign=%s",
            self._merchant_id, amount_str, self._CURRENCY, order_id, sign,
        )
        return PaymentResult(
            provider_payment_id=order_id,
            payment_url=url,
        )

    # ── webhook verification ──────────────────────────────────────────────────

    async def verify_webhook(
        self,
        body: bytes,
        headers: dict[str, str],
    ) -> IncomingPayment | None:
        try:
            params = {k: v[0] for k, v in parse_qs(body.decode()).items()}

            amount   = params.get("AMOUNT", "")
            order_id = params.get("MERCHANT_ORDER_ID", "")
            sign_got = params.get("SIGN", "").lower()

            if not amount or not order_id or not sign_got:
                logger.warning(
                    "FreeKassa webhook: missing required fields. Got keys: %s",
                    list(params.keys()),
                )
                return None

            sign_expected = self._sign_notify(amount, order_id)
            if not hmac.compare_digest(sign_expected, sign_got):
                logger.error(
                    "FreeKassa notify signature mismatch: expected=%s got=%s "
                    "merchant=%s amount=%s order=%s",
                    sign_expected, sign_got,
                    self._merchant_id, amount, order_id,
                )
                return None

            logger.info(
                "FreeKassa notify verified: order=%s amount=%s intid=%s",
                order_id, amount, params.get("intid"),
            )
            return IncomingPayment(
                our_payment_id=int(order_id),
                provider_payment_id=params.get("intid", order_id),
                amount=float(amount),
                currency=self._CURRENCY,
            )
        except Exception as exc:
            logger.error("FreeKassa webhook parse error: %s", exc, exc_info=True)
            return None
