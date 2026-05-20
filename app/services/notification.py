from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def notify_admin(text: str) -> None:
    url = f"https://api.telegram.org/bot{settings.BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": settings.ADMIN_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                logger.error("Admin notification failed: %s %s", response.status_code, response.text)
    except Exception as exc:
        logger.error("Admin notification error: %s", exc)
