from __future__ import annotations

import hashlib
import hmac
import logging
import urllib.parse
from urllib.parse import unquote, urlencode

import httpx

from app.payments.provider_base import IncomingPayment, PaymentProviderBase, PaymentResult

logger = logging.getLogger(__name__)

_API_BASE = "https://yoomoney.ru/api"

# SHA1 signature formula (YooMoney QuickPay notification):
# SHA1("notification_type&operation_id&amount&currency&datetime&sender&codepro&secret&label")
# Docs: https://yoomoney.ru/docs/payment-buttons/using-api/notifications


def _parse_body(body: bytes) -> dict[str, str]:
    """Parse x-www-form-urlencoded body.

    Uses unquote() (not unquote_plus) so '+' in datetime timezone offsets
    (sent as %2B by YooMoney) are decoded to '+', never to space.
    Empty-valued fields (e.g. sender= for card payments) are kept as "".
    """
    result: dict[str, str] = {}
    for pair in body.decode("utf-8", errors="replace").split("&"):
        if "=" in pair:
            k, _, v = pair.partition("=")
            result[unquote(k)] = unquote(v)
        elif pair:
            result[unquote(pair)] = ""
    return result


class YooMoneyProvider(PaymentProviderBase):
    """YooMoney wallet-mode (QuickPay) provider.

    Payment flow:
      User clicks link → YooMoney QuickPay form (wallet OR card) →
      YooMoney sends HTTP POST notification to our webhook URL.

    REQUIRED setup in YooMoney LK (wallet.yoomoney.ru):
      Настройки → Переводы → HTTP-уведомления
        URL: http://<your-domain>/webhooks/payment/yoomoney
        Тип: p2p (входящие переводы)
        Секрет: any string — MUST match YOOMONEY_NOTIFICATION_SECRET here

    Config keys:
      YOOMONEY_WALLET               – wallet number (4100...)
      YOOMONEY_NOTIFICATION_SECRET  – secret set in YooMoney LK notification settings
      YOOMONEY_ACCESS_TOKEN         – (optional) OAuth token for API status polling
    """

    def __init__(
        self,
        wallet: str,
        notification_secret: str,
        access_token: str = "",
    ) -> None:
        self._wallet = wallet.strip()
        self._secret = notification_secret.strip()   # strip is critical — extra whitespace breaks SHA1
        self._access_token = access_token.strip()

    # ── payment creation ──────────────────────────────────────────────────────

    async def create_payment(
        self,
        our_payment_id: int,
        amount: float,
        currency: str,
        description: str,
        user_id: int,
    ) -> PaymentResult:
        label = f"pl_{our_payment_id}"
        params = {
            "receiver":      self._wallet,
            "quickpay-form": "shop",          # shows both wallet + bank card options
            "targets":       f"Private Line #{our_payment_id}",
            "sum":           f"{amount:.2f}",
            "label":         label,
            "successURL":    "https://t.me/",  # redirect back to Telegram after payment
        }
        url = "https://yoomoney.ru/quickpay/confirm?" + urlencode(params, quote_via=urllib.parse.quote)
        logger.info("YooMoney payment URL: wallet=%s label=%s amount=%.2f", self._wallet, label, amount)
        return PaymentResult(
            provider_payment_id=label,
            payment_url=url,
            display_data={"wallet": self._wallet, "label": label, "amount": amount},
        )

    # ── webhook verification ──────────────────────────────────────────────────

    async def verify_webhook(
        self,
        body: bytes,
        headers: dict[str, str],
    ) -> IncomingPayment | None:
        try:
            params = _parse_body(body)

            notification_type = params.get("notification_type", "")
            operation_id      = params.get("operation_id", "")
            amount            = params.get("amount", "")
            currency          = params.get("currency", "")
            dt                = params.get("datetime", "")
            sender            = params.get("sender", "")      # empty for card payments
            codepro           = params.get("codepro", "false")
            label             = params.get("label", "")
            sha1_got          = params.get("sha1_hash", "").lower().strip()
            unaccepted        = params.get("unaccepted", "false").lower()

            logger.info(
                "YooMoney webhook received: type=%s label=%s amount=%s operation=%s "
                "unaccepted=%s codepro=%s secret_len=%d",
                notification_type, label, amount, operation_id,
                unaccepted, codepro, len(self._secret),
            )

            # ── Reject payments that are "unaccepted" (held by YooMoney, not yet released)
            if unaccepted == "true":
                logger.warning(
                    "YooMoney: payment %s is unaccepted (held), skipping auto-confirm",
                    operation_id,
                )
                return None

            # ── Validate SHA1 signature
            if not sha1_got:
                logger.error("YooMoney webhook: sha1_hash is missing. Params: %s", list(params.keys()))
                return None

            check_str = (
                f"{notification_type}&{operation_id}&{amount}&{currency}"
                f"&{dt}&{sender}&{codepro}&{self._secret}&{label}"
            )
            sha1_expected = hashlib.sha1(check_str.encode("utf-8")).hexdigest()

            if not hmac.compare_digest(sha1_expected, sha1_got):
                logger.error(
                    "YooMoney SHA1 mismatch: expected=%s got=%s | "
                    "label=%s amount=%s secret_len=%d | "
                    "check_str=%r",
                    sha1_expected, sha1_got,
                    label, amount, len(self._secret),
                    check_str,
                )
                return None

            # ── Extract our payment ID from label (format: "pl_{id}")
            if not label.startswith("pl_"):
                logger.warning("YooMoney: unexpected label format %r, skipping", label)
                return None

            try:
                our_payment_id = int(label[3:])
            except ValueError:
                logger.error("YooMoney: cannot parse payment ID from label %r", label)
                return None

            logger.info(
                "YooMoney webhook verified OK: payment_id=%d operation=%s amount=%s type=%s",
                our_payment_id, operation_id, amount, notification_type,
            )
            return IncomingPayment(
                our_payment_id=our_payment_id,
                provider_payment_id=operation_id,
                amount=float(amount),
                currency="RUB",
            )

        except Exception as exc:
            logger.error("YooMoney webhook parse error: %s", exc, exc_info=True)
            return None

    # ── fallback: API polling ─────────────────────────────────────────────────

    async def check_payment_by_label(self, label: str, expected_amount: float) -> str | None:
        """Poll YooMoney operation-history API to find payment by label.

        Requires YOOMONEY_ACCESS_TOKEN (OAuth).
        Returns operation_id on success, None if not found or not confirmed.
        """
        if not self._access_token:
            logger.warning("YooMoney: access_token not set — API polling skipped for label=%s", label)
            return None

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{_API_BASE}/operation-history",
                    data={"label": label, "type": "deposition"},
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
            if resp.status_code != 200:
                logger.error("YooMoney API error %d for label=%s: %s", resp.status_code, label, resp.text[:300])
                return None

            data = resp.json()
            ops = data.get("operations", [])
            logger.debug("YooMoney API: found %d operations for label=%s", len(ops), label)

            for op in ops:
                if (
                    op.get("label") == label
                    and op.get("status") == "success"
                    and op.get("direction") == "in"
                    and float(op.get("amount", 0)) >= expected_amount * 0.97   # allow up to 3% fee
                ):
                    op_id = op.get("operation_id", label)
                    logger.info("YooMoney API confirmed: label=%s operation=%s amount=%s", label, op_id, op.get("amount"))
                    return op_id

            logger.info("YooMoney API: no confirmed payment for label=%s (ops=%d)", label, len(ops))
        except Exception as exc:
            logger.error("YooMoney API poll error label=%s: %s", label, exc)

        return None
