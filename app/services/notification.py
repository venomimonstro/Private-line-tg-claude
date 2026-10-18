from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)
_STATE_FILE = Path(settings.APP_DIR) / "data" / "admin_notification_state.json"


def _notification_id(text: str, dedupe_key: str | None) -> str:
    return hashlib.sha256((dedupe_key or text).encode("utf-8")).hexdigest()


def _claim_notification(notification_id: str, cooldown_seconds: int) -> tuple[bool, float]:
    """Atomically claim one alert across admin, bot and maintenance processes."""
    now = time.time()
    state_file = _STATE_FILE
    state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state_file.with_suffix(".lock")
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            raw = json.loads(state_file.read_text()) if state_file.is_file() else {}
            state = {str(key): float(value) for key, value in raw.items()}
        except (OSError, ValueError, TypeError, AttributeError):
            state = {}
        last = state.get(notification_id, 0.0)
        if cooldown_seconds > 0 and now - last < cooldown_seconds:
            return False, last
        # Keep this operational file bounded even after years of alerts.
        state = {key: value for key, value in state.items() if now - value < 86400 * 7}
        state[notification_id] = now
        fd, temporary = tempfile.mkstemp(prefix="admin-notify.", dir=state_file.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream, separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, state_file)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True, now


def _release_notification(notification_id: str, claimed_at: float) -> None:
    """Permit a retry when both Telegram transports failed."""
    state_file = _STATE_FILE
    lock_path = state_file.with_suffix(".lock")
    try:
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads(state_file.read_text()) if state_file.is_file() else {}
            if float(state.get(notification_id, 0.0)) != claimed_at:
                return
            state.pop(notification_id, None)
            state_file.write_text(json.dumps(state, separators=(",", ":"), sort_keys=True))
            os.chmod(state_file, 0o600)
    except (OSError, ValueError, TypeError, AttributeError):
        logger.warning("Could not release failed admin notification claim")


async def notify_admin(
    text: str, *, dedupe_key: str | None = None, cooldown_seconds: int = 300
) -> None:
    """Send one bounded admin alert and suppress duplicate notification storms."""
    notification_id = _notification_id(text, dedupe_key)
    try:
        claimed, claimed_at = _claim_notification(notification_id, max(0, cooldown_seconds))
    except OSError as exc:
        # Notification state must never take down payments or the bot.
        logger.error("Admin notification dedupe unavailable: %s", exc)
        claimed, claimed_at = True, 0.0
    if not claimed:
        logger.info("Suppressed duplicate admin notification %s", notification_id[:12])
        return
    sent = await _try_send(settings.BOT_TOKEN, text)
    if not sent and settings.BACKUP_BOT_TOKEN:
        sent = await _try_send(settings.BACKUP_BOT_TOKEN, text)
    if not sent and claimed_at:
        _release_notification(notification_id, claimed_at)


async def notify_user(telegram_id: int, text: str) -> None:
    """Send a message to a specific Telegram user. Never raises."""
    await _try_send_to(settings.BOT_TOKEN, telegram_id, text)


async def _try_send(token: str, text: str) -> bool:
    return await _try_send_to(token, settings.ADMIN_CHAT_ID, text)


async def _try_send_to(token: str, chat_id: int | str, text: str) -> bool:
    base = settings.TELEGRAM_API_URL.rstrip("/") if settings.TELEGRAM_API_URL else "https://api.telegram.org"
    url = f"{base}/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json={
                "chat_id": chat_id,
                "text": text[:4096],
                "parse_mode": "HTML",
            })
        return resp.status_code == 200
    except Exception as exc:
        logger.error("_try_send_to chat_id=%s failed: %s", chat_id, exc)
        return False
