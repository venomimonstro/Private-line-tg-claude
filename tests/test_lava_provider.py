import hashlib
import hmac
import json
import unittest
from unittest.mock import AsyncMock

import httpx

from app.payments.providers.lava import LavaProvider


class LavaProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = LavaProvider("shop-1", "api-secret", "webhook-secret")

    async def asyncTearDown(self):
        await self.provider.aclose()

    def test_signed_body_signature_matches_exact_bytes(self):
        payload = {"sum": 100.0, "orderId": "42", "shopId": "shop-1"}
        body, signature = self.provider._signed_body(payload)
        expected = hmac.new(
            b"api-secret", body, hashlib.sha256
        ).hexdigest()
        self.assertEqual(signature, expected)
        self.assertEqual(json.loads(body), payload)

    async def test_webhook_uses_additional_secret(self):
        payload = {
            "status": "success",
            "orderId": "42",
            "invoiceId": "inv-1",
            "amount": 100.0,
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        signature = hmac.new(
            b"webhook-secret", body, hashlib.sha256
        ).hexdigest()

        incoming = await self.provider.verify_webhook(
            body, {"signature": signature}
        )
        self.assertIsNotNone(incoming)
        self.assertEqual(incoming.our_payment_id, 42)

        wrong = hmac.new(b"api-secret", body, hashlib.sha256).hexdigest()
        self.assertIsNone(
            await self.provider.verify_webhook(body, {"signature": wrong})
        )

    async def test_status_query_is_bound_to_merchant_order_id(self):
        response = httpx.Response(
            200,
            json={
                "status": 200,
                "status_check": True,
                "data": {
                    "status": "success",
                    "orderId": "42",
                    "id": "inv-1",
                    "sum": 100.0,
                },
            },
            request=httpx.Request(
                "POST", "https://api.lava.ru/business/invoice/status"
            ),
        )
        self.provider._http.post = AsyncMock(return_value=response)

        data = await self.provider.get_invoice_status_by_order_id("42")

        self.assertEqual(data["orderId"], "42")
        args, kwargs = self.provider._http.post.await_args
        raw = kwargs["content"]
        self.assertEqual(json.loads(raw), {"shopId": "shop-1", "orderId": "42"})
        expected = hmac.new(b"api-secret", raw, hashlib.sha256).hexdigest()
        self.assertEqual(kwargs["headers"]["Signature"], expected)


if __name__ == "__main__":
    unittest.main()
