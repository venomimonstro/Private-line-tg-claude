from __future__ import annotations
import secrets
import logging
from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import select
from app.database import async_session_maker
from app.models.user import User
from app.config import settings

logger = logging.getLogger(__name__)
router = Router(name="portal_link")

def _generate_token() -> str:
    return secrets.token_urlsafe(32)

@router.message(F.text == "🌐 Веб-портал")
async def send_portal_link(message: Message, db_user: User) -> None:
    base_url = settings.WEBHOOK_BASE_URL.rstrip("/")
    if not base_url:
        await message.answer(
            "⚠️ Веб-портал временно недоступен. Воспользуйтесь ботом для управления подпиской."
        )
        return

    async with async_session_maker() as session:
        user = await session.get(User, db_user.id)
        if user is None:
            return
        if not user.portal_token:
            user.portal_token = _generate_token()
            await session.commit()
        token = user.portal_token

    portal_url = f"{base_url}/portal/me/{token}"
    await message.answer(
        f"🌐 <b>Ваш личный веб-портал</b>\n\n"
        f"Откройте ссылку в браузере. Сохраните её в закладки — она даёт полный доступ к вашему аккаунту без Telegram.\n\n"
        f"<code>{portal_url}</code>\n\n"
        f"⚠️ <i>Не передавайте ссылку другим людям — по ней доступен ваш VPN-ключ.</i>",
        parse_mode="HTML",
    )
