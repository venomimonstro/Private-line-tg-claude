from __future__ import annotations

import hashlib
import hmac
import logging
import urllib.parse
from urllib.parse import parse_qs

import httpx

from app.payments.provider_base import IncomingPayment, PaymentProviderBase, PaymentResult

logger = logging.getLogger(__name__)

_API_BASE = "https://yoomoney.ru/api"


class YooMoneyProvider(PaymentProviderBase):
    """YooMoney wallet-mode provider.

    Primary:   HTTP notification webhook (push from YooMoney).
    Fallback:  OAuth API polling via access_token (operation-history by label).
    Config: YOOMONEY_WALLET, YOOMONEY_NOTIFICATION_SECRET, YOOMONEY_ACCESS_TOKEN
    """

    def __init__(self, wallet: str, notification_secret: str, access_token: str = "") -> None:
        self._wallet = wallet
        self._secret = notification_secret
        self._access_token = access_token

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
            params = {k: v[0].strip() for k, v in parse_qs(body.decode()).items()}
            notification_type = params.get("notification_type", "")
            operation_id = params.get("operation_id", "")
            amount = params.get("amount", "")
            currency = params.get("currency", "")
            dt = params.get("datetime", "")
            sender = params.get("sender", "")
            codepro = params.get("codepro", "")
            label = params.get("label", "")
            sha1_hash = params.get("sha1_hash", "")
            logger.info(
                "YooMoney webhook: type=%s label=%s amount=%s secret_len=%d",
                notification_type, label, amount, len(self._secret),
            )

            check_str = (
                f"{notification_type}&{operation_id}&{amount}&{currency}"
                f"&{dt}&{sender}&{codepro}&{self._secret}&{label}"
            )
            expected = hashlib.sha1(check_str.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(expected, sha1_hash):
                logger.error(
                    "YooMoney signature mismatch: expected=%s got=%s label=%s secret_len=%d",
                    expected, sha1_hash, label, len(self._secret),
                )
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
        except Exception as exc:
            logger.error("YooMoney webhook parse error: %s", exc)
            return None

    async def check_payment_by_label(self, label: str, expected_amount: float) -> str | None:
        """Poll YooMoney API to check if payment with given label was received.

        Returns operation_id if found and successful, None otherwise.
        Requires YOOMONEY_ACCESS_TOKEN configured in integrations.
        """
        if not self._access_token:
            logger.warning("YooMoney: access_token not configured, cannot poll payment status")
            return None

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{_API_BASE}/operation-history",
                    data={"label": label},
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
            if resp.status_code != 200:
                logger.error("YooMoney API error %d: %s", resp.status_code, resp.text[:200])
                return None

            data = resp.json()
            for op in data.get("operations", []):
                if (
                    op.get("label") == label
                    and op.get("status") == "success"
                    and op.get("direction") == "in"
                    and float(op.get("amount", 0)) >= expected_amount * 0.98  # 2% fee tolerance
                ):
                    logger.info("YooMoney API confirmed payment label=%s op=%s", label, op.get("operation_id"))
                    return op.get("operation_id", label)
        except Exception as exc:
            logger.error("YooMoney API poll error label=%s: %s", label, exc)

        return None
