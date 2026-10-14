"""Subscription tokens used by the Telegram bot and the /sub/ endpoint."""
import base64
import hashlib
import hmac
import unittest

from app.services.sub_token import make_sub_token, verify_sub_token


class SubscriptionTokenTests(unittest.TestCase):
    SECRET = "production-test-secret"

    def test_issued_token_round_trips(self):
        for telegram_id in (1, 123456789, 9876543210123):
            token = make_sub_token(telegram_id, self.SECRET)
            self.assertEqual(verify_sub_token(token, self.SECRET), telegram_id)

    def test_previous_short_signature_still_verifies(self):
        telegram_id = 123456789
        encoded = base64.urlsafe_b64encode(str(telegram_id).encode()).rstrip(b"=").decode()
        signature = hmac.new(self.SECRET.encode(), str(telegram_id).encode(), hashlib.sha256).hexdigest()[:12]
        self.assertEqual(verify_sub_token(f"{encoded}.{signature}", self.SECRET), telegram_id)

    def test_tampered_and_noncanonical_tokens_are_rejected(self):
        token = make_sub_token(123456789, self.SECRET)
        self.assertIsNone(verify_sub_token(token, "another-secret"))
        self.assertIsNone(verify_sub_token(token.replace(".", "!", 1), self.SECRET))
        encoded, signature = token.split(".")
        self.assertIsNone(verify_sub_token(f"{encoded}!.{signature}", self.SECRET))
        leading_zero = base64.urlsafe_b64encode(b"0123456789").rstrip(b"=").decode()
        self.assertIsNone(verify_sub_token(f"{leading_zero}.{signature}", self.SECRET))


if __name__ == "__main__":
    unittest.main()
