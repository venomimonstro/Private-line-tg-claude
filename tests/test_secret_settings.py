import os
import unittest

from cryptography.fernet import Fernet

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("ADMIN_CHAT_ID", "1")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("SECRET_KEY", "test")

from app.services.secret_settings import (
    decode_setting_value,
    encode_setting_value,
    is_encrypted_setting_value,
)


class SecretSettingsTests(unittest.TestCase):
    def test_sensitive_value_is_encrypted_and_round_trips(self):
        encoded = encode_setting_value("YOOKASSA_SECRET_KEY", "super-secret")
        self.assertTrue(encoded.startswith("enc:v1:"))
        self.assertNotIn("super-secret", encoded)
        self.assertEqual(
            decode_setting_value("YOOKASSA_SECRET_KEY", encoded),
            "super-secret",
        )

    def test_non_secret_value_remains_plain(self):
        value = encode_setting_value("YOOKASSA_SHOP_ID", "123456")
        self.assertEqual(value, "123456")
        self.assertEqual(decode_setting_value("YOOKASSA_SHOP_ID", value), "123456")

    def test_legacy_plaintext_secret_remains_readable_for_migration(self):
        self.assertEqual(
            decode_setting_value("LAVA_WEBHOOK_SECRET", "legacy-secret"),
            "legacy-secret",
        )
        self.assertFalse(
            is_encrypted_setting_value("LAVA_WEBHOOK_SECRET", "legacy-secret")
        )

    def test_already_encrypted_value_is_not_double_encrypted(self):
        first = encode_setting_value("CRYPTO_BOT_TOKEN", "abc")
        second = encode_setting_value("CRYPTO_BOT_TOKEN", first)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
