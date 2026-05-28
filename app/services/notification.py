from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def notify_admin(text: str) -> None:
    """Send notification to ADMIN_CHAT_ID. Tries backup bot if main fails. Never raises."""
    sent = await _try_send(settings.BOT_TOKEN, text)
    if not sent and settings.BACKUP_BOT_TOKEN:
        await _try_send(settings.BACKUP_BOT_TOKEN, text)


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
