from __future__ import annotations

import hashlib
import hmac as hmac_module
import logging
import urllib.parse
from urllib.parse import unquote, urlencode

import httpx

from app.payments.provider_base import IncomingPayment, PaymentProviderBase, PaymentResult

logger = logging.getLogger(__name__)

_API_BASE = "https://yoomoney.ru/api"

# YooMoney sends notifications in TWO formats depending on account/LK version:
#
# OLD format: field sha1_hash, formula:
#   SHA1("notification_type&operation_id&amount&currency&datetime&sender&codepro&secret&label")
#
# NEW format: field sign, formula:
#   HMAC-SHA256(secret, "key1=val1&key2=val2&..." sorted alphabetically, all params except sign)
#
# Logs showed NEW format with bill_id, operation_label, sign — this is the current YooMoney API.


def _parse_body(body: bytes) -> dict[str, str]:
    """Parse application/x-www-form-urlencoded, keeping values decoded.
    Empty-valued fields (e.g. sender= for card payments) are kept as "".
    Uses unquote() — not unquote_plus() — so %2B in datetime stays as '+'.
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

    REQUIRED one-time setup in YooMoney LK (yoomoney.ru → Настройки → Переводы → HTTP-уведомления):
      URL:    http://<your-domain>/webhooks/payment/yoomoney
      Секрет: any string — MUST match YOOMONEY_NOTIFICATION_SECRET in Admin → Integrations

    Config keys:
      YOOMONEY_WALLET               – wallet number (4100...)
      YOOMONEY_NOTIFICATION_SECRET  – secret set in YooMoney LK notification settings
      YOOMONEY_ACCESS_TOKEN         – (optional) OAuth token for API status polling fallback
    """

    def __init__(self, wallet: str, notification_secret: str, access_token: str = "") -> None:
        self._wallet = wallet.strip()
        self._secret = notification_secret.strip()  # strip is critical — whitespace breaks signatures
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
            "quickpay-form": "shop",       # shows wallet + bank card options
            "targets":       f"Private Line #{our_payment_id}",
            "sum":           f"{amount:.2f}",
            "label":         label,
            "successURL":    "https://t.me/",
        }
        url = "https://yoomoney.ru/quickpay/confirm?" + urlencode(params, quote_via=urllib.parse.quote)
        logger.info("YooMoney payment URL: wallet=%s label=%s amount=%.2f", self._wallet, label, amount)
        return PaymentResult(
            provider_payment_id=label,
            payment_url=url,
            display_data={"wallet": self._wallet, "label": label, "amount": amount},
        )

    # ── webhook verification ──────────────────────────────────────────────────

    async def verify_webhook(self, body: bytes, headers: dict[str, str]) -> IncomingPayment | None:
        try:
            params = _parse_body(body)

            notification_type = params.get("notification_type", "")
            operation_id      = params.get("operation_id", "")
            amount            = params.get("amount", "")
            label             = params.get("label", "")
            unaccepted        = params.get("unaccepted", "false").lower()
            is_test           = "test_notification" in params

            logger.info(
                "YooMoney webhook: type=%s label=%s amount=%s operation=%s "
                "unaccepted=%s test=%s fields=%s",
                notification_type, label, amount, operation_id,
                unaccepted, is_test, sorted(params.keys()),
            )

            if is_test:
                logger.info("YooMoney: test notification, skipping payment processing")
                return None

            if unaccepted == "true":
                logger.warning("YooMoney: payment %s is unaccepted (held), skipping", operation_id)
                return None

            # ── try NEW format first (sign / HMAC-SHA256) ────────────────────
            if "sign" in params:
                if not self._verify_sign_new(params):
                    return None
            # ── fall back to OLD format (sha1_hash / SHA1) ───────────────────
            elif "sha1_hash" in params:
                if not self._verify_sign_old(params):
                    return None
            else:
                logger.error(
                    "YooMoney webhook: no signature field (sign or sha1_hash). fields=%s",
                    sorted(params.keys()),
                )
                return None

            # ── extract payment ID from label (format: pl_{id}) ──────────────
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
            logger.error("YooMoney webhook error: %s", exc, exc_info=True)
            return None

    def _verify_sign_new(self, params: dict[str, str]) -> bool:
        """NEW format: HMAC-SHA256 of all params except 'sign', sorted alphabetically."""
        sign_got = params.get("sign", "").lower().strip()
        # YooMoney excludes empty-value params from signature (e.g. bill_id=, sender=)
        filtered = sorted((k, v) for k, v in params.items() if k != "sign" and v != "")
        check_str = "&".join(f"{k}={v}" for k, v in filtered)
        expected = hmac_module.new(
            self._secret.encode("utf-8"),
            check_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        ok = hmac_module.compare_digest(expected, sign_got)
        if not ok:
            logger.error(
                "YooMoney HMAC-SHA256 mismatch: expected=%s got=%s secret_len=%d check_str=%r",
                expected, sign_got, len(self._secret), check_str,
            )
        return ok

    def _verify_sign_old(self, params: dict[str, str]) -> bool:
        """OLD format: SHA1 of fixed-order fields."""
        sha1_got = params.get("sha1_hash", "").lower().strip()
        check_str = (
            f"{params.get('notification_type','')}&{params.get('operation_id','')}&"
            f"{params.get('amount','')}&{params.get('currency','')}&"
            f"{params.get('datetime','')}&{params.get('sender','')}&"
            f"{params.get('codepro','')}&{self._secret}&{params.get('label','')}"
        )
        expected = hashlib.sha1(check_str.encode("utf-8")).hexdigest()
        ok = hmac_module.compare_digest(expected, sha1_got)
        if not ok:
            logger.error(
                "YooMoney SHA1 mismatch: expected=%s got=%s secret_len=%d",
                expected, sha1_got, len(self._secret),
            )
        return ok

    # ── fallback: API polling ─────────────────────────────────────────────────

    async def check_payment_by_label(self, label: str, expected_amount: float) -> str | None:
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
                logger.error("YooMoney API %d for label=%s: %s", resp.status_code, label, resp.text[:300])
                return None
            data = resp.json()
            for op in data.get("operations", []):
                if (
                    op.get("label") == label
                    and op.get("status") == "success"
                    and op.get("direction") == "in"
                    and float(op.get("amount", 0)) >= expected_amount * 0.97
                ):
                    op_id = op.get("operation_id", label)
                    logger.info("YooMoney API confirmed: label=%s op=%s", label, op_id)
                    return op_id
        except Exception as exc:
            logger.error("YooMoney API poll error label=%s: %s", label, exc)
        return None
