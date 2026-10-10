import hashlib
import hmac
import unittest
from urllib.parse import quote, urlencode

from app.payments.providers.yoomoney import YooMoneyProvider


class YooMoneyWebhookTests(unittest.IsolatedAsyncioTestCase):
    SECRET = "test-notification-secret"

    def _signed_body(self, **overrides) -> bytes:
        params = {
            "notification_type": "card-incoming",
            "operation_id": "904035776918098009",
            "amount": "97.00",
            "withdraw_amount": "100.00",
            "currency": "643",
            "datetime": "2026-10-10T17:00:00Z",
            "sender": "",
            "codepro": "false",
            "label": "pl_42",
            "unaccepted": "false",
            "test_notification": "false",
        }
        params.update(overrides)
        check = "&".join(
            f"{key}={quote(value, safe='')}"
            for key, value in sorted(params.items())
        )
        params["sign"] = hmac.new(
            self.SECRET.encode(),
            check.encode(),
            hashlib.sha256,
        ).hexdigest()
        return urlencode(params).encode()

    async def test_false_test_notification_is_a_real_payment(self):
        provider = YooMoneyProvider("410000000000", self.SECRET)
        incoming = await provider.verify_webhook(self._signed_body(), {})
        self.assertIsNotNone(incoming)
        self.assertEqual(incoming.our_payment_id, 42)
        # Order authorization uses the sender debit, not net wallet settlement.
        self.assertEqual(incoming.amount, 100.0)
        self.assertEqual(incoming.currency, "RUB")

    async def test_true_test_notification_is_ignored(self):
        provider = YooMoneyProvider("410000000000", self.SECRET)
        incoming = await provider.verify_webhook(
            self._signed_body(test_notification="true"), {}
        )
        self.assertIsNone(incoming)

    async def test_unexpected_currency_is_rejected(self):
        provider = YooMoneyProvider("410000000000", self.SECRET)
        incoming = await provider.verify_webhook(
            self._signed_body(currency="840"), {}
        )
        self.assertIsNone(incoming)

    async def test_underpayment_is_visible_as_sender_debit(self):
        provider = YooMoneyProvider("410000000000", self.SECRET)
        incoming = await provider.verify_webhook(
            self._signed_body(withdraw_amount="97.00", amount="94.09"), {}
        )
        self.assertIsNotNone(incoming)
        self.assertEqual(incoming.amount, 97.0)


if __name__ == "__main__":
    unittest.main()
