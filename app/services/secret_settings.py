"""Encrypted AppSetting helpers for payment/provider credentials.

Only explicitly sensitive keys are encrypted. Legacy plaintext values remain
readable until the idempotent migration rewrites them, so deployment does not
break existing integrations.
"""
from __future__ import annotations

from app.encryption import decrypt, encrypt

_PREFIX = "enc:v1:"

SECRET_SETTING_KEYS = frozenset({
    "YOOKASSA_SECRET_KEY",
    "YOOMONEY_NOTIFICATION_SECRET",
    "YOOMONEY_ACCESS_TOKEN",
    "FREEKASSA_SECRET_1",
    "FREEKASSA_SECRET_2",
    "FREEKASSA_API_KEY",
    "LAVA_SECRET_KEY",
    "LAVA_WEBHOOK_SECRET",
    "ROBOKASSA_PASSWORD_1",
    "ROBOKASSA_PASSWORD_2",
    "PRODAMUS_WEBHOOK_SECRET",
    "CRYPTO_BOT_TOKEN",
})


def is_secret_setting(key: str) -> bool:
    return key in SECRET_SETTING_KEYS


def encode_setting_value(key: str, value: str) -> str:
    if not value or not is_secret_setting(key):
        return value
    if value.startswith(_PREFIX):
        # Never double-encrypt a value already produced by this helper.
        decode_setting_value(key, value)
        return value
    return _PREFIX + encrypt(value)


def decode_setting_value(key: str, value: str | None) -> str:
    raw = value or ""
    if not raw or not is_secret_setting(key):
        return raw
    if raw.startswith(_PREFIX):
        return decrypt(raw[len(_PREFIX):])
    # Legacy plaintext compatibility. migrate_secret_settings rewrites it.
    return raw


def is_encrypted_setting_value(key: str, value: str | None) -> bool:
    return bool(value) and is_secret_setting(key) and str(value).startswith(_PREFIX)
