"""Subscription URL token utilities — shared between bot and admin."""
from __future__ import annotations

import base64
import hashlib
import hmac

# New links use 128 bits of HMAC. Keep verification of the historical 48-bit
# format during migration so already-imported VPN subscriptions do not break.
_SIG_HEX_LEN = 32
_LEGACY_SIG_HEX_LEN = 12


def make_sub_token(telegram_id: int, secret: str) -> str:
    """Return a stable, verifiable URL-safe token for the given Telegram user."""
    tid = str(telegram_id).encode()
    tid_b64 = base64.urlsafe_b64encode(tid).rstrip(b"=").decode()
    sig = hmac.new(secret.encode(), tid, hashlib.sha256).hexdigest()[:_SIG_HEX_LEN]
    return f"{tid_b64}.{sig}"


def verify_sub_token(token: str, secret: str) -> int | None:
    """Return telegram_id if token is valid, else None.

    Both the current 128-bit signature and the legacy 48-bit signature are
    accepted for compatibility. New links are always minted with 128 bits.
    """
    try:
        if "." not in token:
            return None
        tid_b64, sig = token.split(".", 1)
        if len(sig) not in (_SIG_HEX_LEN, _LEGACY_SIG_HEX_LEN):
            return None
        pad = (-len(tid_b64)) % 4
        if pad:
            tid_b64 += "=" * pad
        tid = base64.b64decode(tid_b64, altchars=b"-_", validate=True)
        telegram_id = int(tid.decode())
        if str(telegram_id).encode() != tid:
            return None
        full = hmac.new(secret.encode(), tid, hashlib.sha256).hexdigest()
        expected = full[:len(sig)]
        if hmac.compare_digest(sig, expected):
            return telegram_id
        return None
    except Exception:
        return None


def make_subscription_url(telegram_id: int) -> str:
    """Return the HTTPS auto-update subscription URL shown to the VPN user."""
    from app.config import settings

    base = settings.CONTROL_PLANE_PUBLIC_URL.strip().rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("CONTROL_PLANE_PUBLIC_URL must be configured with HTTPS")
    token = make_sub_token(telegram_id, settings.SECRET_KEY)
    return f"{base}/sub/{token}"
