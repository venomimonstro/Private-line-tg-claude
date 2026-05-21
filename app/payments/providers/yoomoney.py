from __future__ import annotations

import hashlib
import hmac
import urllib.parse
from urllib.parse import parse_qs

from app.payments.provider_base import IncomingPayment, PaymentProviderBase, PaymentResult


class YooMoneyProvider(PaymentProviderBase):
    """YooMoney wallet-mode provider.

    User transfers to the wallet; we receive HTTP notification with SHA1 signature.
    Config: YOOMONEY_WALLET, YOOMONEY_NOTIFICATION_SECRET
    """

    def __init__(self, wallet: str, notification_secret: str) -> None:
        self._wallet = wallet
        self._secret = notification_secret

    async def create_payment(
        self,
        our_payment_id: int,
        amount: float,
        currency: str,
        description: str,
        user_id: int,
    ) -> PaymentResult:
        label = f"pl_{our_payment_id}"
        targets = urllib.parse.quote(f"Оплата #{our_payment_id}")
        url = (
            f"https://yoomoney.ru/quickpay/confirm"
            f"?receiver={self._wallet}"
            f"&quickpay-form=button"
            f"&targets={targets}"
            f"&sum={amount:.2f}"
            f"&label={label}"
        )
        return PaymentResult(
            provider_payment_id=label,
            payment_url=url,
            display_data={"wallet": self._wallet, "label": label, "amount": amount},
        )

    async def verify_webhook(
        self,
        body: bytes,
        headers: dict[str, str],
    ) -> IncomingPayment | None:
        try:
            params = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            notification_type = params.get("notification_type", "")
            operation_id = params.get("operation_id", "")
            amount = params.get("amount", "")
            currency = params.get("currency", "")
            dt = params.get("datetime", "")
            sender = params.get("sender", "")
            codepro = params.get("codepro", "")
            label = params.get("label", "")
            sha1_hash = params.get("sha1_hash", "")

            check_str = (
                f"{notification_type}&{operation_id}&{amount}&{currency}"
                f"&{dt}&{sender}&{codepro}&{self._secret}&{label}"
            )
            expected = hashlib.sha1(check_str.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(expected, sha1_hash):
                return None

            if not label.startswith("pl_"):
                return None
            our_payment_id = int(label[3:])
            return IncomingPayment(
                our_payment_id=our_payment_id,
                provider_payment_id=operation_id,
                amount=float(amount),
                currency="RUB",
            )
        except Exception:
            return None
