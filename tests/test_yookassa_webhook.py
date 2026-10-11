import json
import unittest
from unittest.mock import AsyncMock

import httpx

from app.payments.providers.yookassa import YooKassaProvider


class YooKassaWebhookTests(unittest.IsolatedAsyncioTestCase):
    def _provider_with_verified(self, payload: dict) -> YooKassaProvider:
        provider = YooKassaProvider("shop", "secret")
        fake_response = httpx.Response(
            200,
            json=payload,
            request=httpx.Request("GET", "https://api.yookassa.ru/v3/payments/pay-1"),
        )
        provider._http.get = AsyncMock(return_value=fake_response)
        self.addAsyncCleanup(provider.aclose)
        return provider

    async def test_webhook_order_id_comes_from_verified_api_not_body(self):
        provider = self._provider_with_verified({
            "id": "pay-1",
            "status": "succeeded",
            "paid": True,
            "amount": {"value": "100.00", "currency": "RUB"},
            "metadata": {"order_id": "42"},
        })
        forged_webhook = {
            "event": "payment.succeeded",
            "object": {
                "id": "pay-1",
                "metadata": {"order_id": "999"},
            },
        }

        incoming = await provider.verify_webhook(
            json.dumps(forged_webhook).encode(), {}
        )

        self.assertIsNotNone(incoming)
        self.assertEqual(incoming.our_payment_id, 42)
        self.assertEqual(incoming.provider_payment_id, "pay-1")
        self.assertEqual(incoming.amount, 100.0)

    async def test_unpaid_api_object_is_rejected(self):
        provider = self._provider_with_verified({
            "id": "pay-1",
            "status": "pending",
            "paid": False,
            "amount": {"value": "100.00", "currency": "RUB"},
            "metadata": {"order_id": "42"},
        })
        body = json.dumps({
            "event": "payment.succeeded",
            "object": {"id": "pay-1"},
        }).encode()

        self.assertIsNone(await provider.verify_webhook(body, {}))

    async def test_verified_non_rub_payment_is_rejected(self):
        provider = self._provider_with_verified({
            "id": "pay-1",
            "status": "succeeded",
            "paid": True,
            "amount": {"value": "100.00", "currency": "USD"},
            "metadata": {"order_id": "42"},
        })
        body = json.dumps({
            "event": "payment.succeeded",
            "object": {"id": "pay-1"},
        }).encode()

        self.assertIsNone(await provider.verify_webhook(body, {}))


if __name__ == "__main__":
    unittest.main()
