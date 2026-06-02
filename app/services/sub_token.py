"""Subscription URL token utilities — shared between bot and admin."""
from __future__ import annotations

import base64
import hashlib
import hmac


def make_sub_token(telegram_id: int, secret: str) -> str:
    """Return a stable, verifiable URL-safe token for the given Telegram user."""
    tid = str(telegram_id).encode()
    tid_b64 = base64.urlsafe_b64encode(tid).rstrip(b"=").decode()
    sig = hmac.new(secret.encode(), tid, hashlib.sha256).hexdigest()[:12]
    return f"{tid_b64}.{sig}"


def verify_sub_token(token: str, secret: str) -> int | None:
    """Return telegram_id if token is valid, else None."""
    try:
        if "." not in token:
            return None
        tid_b64, sig = token.split(".", 1)
        pad = 4 - len(tid_b64) % 4
        if pad != 4:
            tid_b64 += "=" * pad
        tid = base64.urlsafe_b64decode(tid_b64)
        telegram_id = int(tid.decode())
        expected = hmac.new(secret.encode(), tid, hashlib.sha256).hexdigest()[:12]
        if hmac.compare_digest(sig, expected):
            return telegram_id
        return None
    except Exception:
        return None
