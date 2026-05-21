from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.database import async_session_maker
from app.enums import UserStatus
from app.models.user import User
from sqlalchemy import select

logger = logging.getLogger(__name__)

_MAINTENANCE_MSG = (
    "🔧 Сервис выполняет техническое обновление.\n"
    "Действующие подключения продолжают работать.\n"
    "Покупка и продление будут доступны после завершения обновления."
)

_MAINTENANCE_BLOCKED_ACTIONS = {
    "💳 Купить доступ",
    "🔄 Продлить",
}

_MAINTENANCE_BLOCKED_CALLBACK_PREFIXES = (
    "tariff:",
    "accept_rules:",
)


class UserActivityMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from app.config import settings

        telegram_user = data.get("event_from_user")
        if telegram_user is None:
            return await handler(event, data)

        # Rate limit: 20 requests per 10 seconds
        from app.services.rate_limiter import is_rate_limited
        if is_rate_limited(telegram_user.id, max_requests=20, window_seconds=10.0):
            # Silently drop to avoid flooding
            return None

        async with async_session_maker() as session:
            result = await session.execute(
                select(User).where(User.telegram_id == telegram_user.id)
            )
            user = result.scalar_one_or_none()

            if user is None:
                referrer_id: int | None = None
                start_args = data.get("command") and data["command"].args
                if start_args and start_args.startswith("ref_"):
                    try:
                        ref_telegram_id = int(start_args[4:])
                        ref_result = await session.execute(
                            select(User).where(User.telegram_id == ref_telegram_id)
                        )
                        ref_user = ref_result.scalar_one_or_none()
                        if ref_user is not None:
                            referrer_id = ref_user.id
                    except (ValueError, Exception):
                        pass

                user = User(
                    telegram_id=telegram_user.id,
                    username=telegram_user.username,
                    first_name=telegram_user.first_name,
                    last_name=telegram_user.last_name,
                    language_code=telegram_user.language_code if hasattr(telegram_user, "language_code") else None,
                    status=UserStatus.new,
                    referrer_id=referrer_id,
                )
                session.add(user)
                await session.commit()
                await session.refresh(user)
            else:
                user.last_activity_at = datetime.utcnow()
                if telegram_user.username:
                    user.username = telegram_user.username
                if telegram_user.first_name:
                    user.first_name = telegram_user.first_name
                user.last_name = telegram_user.last_name
                if hasattr(telegram_user, "language_code") and telegram_user.language_code:
                    user.language_code = telegram_user.language_code
                await session.commit()

            if user.status == UserStatus.blocked:
                if isinstance(event, Message):
                    await event.answer("🚫 Ваш доступ заблокирован. Обратитесь в поддержку.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("🚫 Ваш доступ заблокирован. Обратитесь в поддержку.", show_alert=True)
                return None

            if settings.MAINTENANCE_MODE:
                if isinstance(event, Message) and event.text in _MAINTENANCE_BLOCKED_ACTIONS:
                    await event.answer(_MAINTENANCE_MSG)
                    return None
                if isinstance(event, CallbackQuery) and event.data:
                    if any(event.data.startswith(prefix) for prefix in _MAINTENANCE_BLOCKED_CALLBACK_PREFIXES):
                        await event.answer(_MAINTENANCE_MSG, show_alert=True)
                        return None

            data["db_user"] = user
            data["db_session"] = session
            return await handler(event, data)
