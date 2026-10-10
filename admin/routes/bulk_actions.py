from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.enums import AdminAction, SubscriptionStatus
from app.models.admin_log import AdminLog
from app.models.subscription import Subscription
from app.models.user import User

router = APIRouter(prefix="/bulk")
templates = Jinja2Templates(directory="admin/templates")

logger = logging.getLogger(__name__)

_TG_MESSAGE_MAX_LEN = 4096
_MESSAGE_PREVIEW_LEN = 100
_SEND_DELAY = 0.05  # 20 msg/sec — safely under Telegram's 30/sec limit


def _parse_user_ids(raw: str) -> list[int]:
    """Parse comma-separated user IDs string into list of ints, ignoring blanks/bad values."""
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                ids.append(int(part))
            except ValueError:
                pass
    return ids


@router.get("", response_class=HTMLResponse)
async def bulk_actions_page(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    return templates.TemplateResponse(
        "bulk_actions.html",
        {
            "request": request,
            "admin": admin,
            "page_title": "Массовые действия",
            "flash": request.query_params.get("flash", ""),
        },
    )


@router.post("/extend")
async def bulk_extend(
    request: Request,
    days: int = Form(...),
    user_ids: str = Form(default=""),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    parsed_ids = _parse_user_ids(user_ids)

    if parsed_ids:
        query = select(Subscription).where(
            Subscription.user_id.in_(parsed_ids),
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
        )
    else:
        query = select(Subscription).where(
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon])
        )

    result = await session.execute(query)
    subscriptions = result.scalars().all()

    from app.services.subscription import extend_subscription

    count = 0
    for sub in subscriptions:
        await extend_subscription(sub, days, session)
        count += 1

    log = AdminLog(
        admin_user_id=admin.id,
        action=AdminAction.bulk_extend_subscriptions,
        details={"days": days, "user_ids": parsed_ids or "all_active", "count": count},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()

    flash = f"Продлено {count} подписок на {days} дней"
    return RedirectResponse(f"/admin/bulk?flash={flash}", status_code=303)


@router.post("/disable")
async def bulk_disable(
    request: Request,
    user_ids: str = Form(...),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    parsed_ids = _parse_user_ids(user_ids)
    if not parsed_ids:
        return RedirectResponse("/admin/bulk?flash=Не указаны пользователи", status_code=303)

    from app.enums import VpnKeyStatus
    from app.models.vpn_key import VpnKey
    from app.services import vpn_key as vpn_key_service
    from sqlalchemy.orm import selectinload

    result = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.vpn_keys).selectinload(VpnKey.server))
        .where(
            Subscription.user_id.in_(parsed_ids),
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
        )
    )
    subscriptions = result.scalars().all()

    count = 0
    pending_remote = 0
    for sub in subscriptions:
        for key in sub.vpn_keys:
            if key.status == VpnKeyStatus.active:
                if key.server is None:
                    key.status = VpnKeyStatus.error
                    pending_remote += 1
                    continue
                disabled = await vpn_key_service.disable_vpn_key(
                    key, key.server, session
                )
                if disabled:
                    key.server.current_users = max(0, key.server.current_users - 1)
                else:
                    pending_remote += 1
        # Control-plane access is hidden immediately; ERROR keys remain visible
        # to integrity audit until remote disable succeeds.
        sub.status = SubscriptionStatus.disabled
        count += 1

    log = AdminLog(
        admin_user_id=admin.id,
        action=AdminAction.bulk_disable_subscriptions,
        details={"user_ids": parsed_ids, "count": count, "pending_remote_disables": pending_remote},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()

    flash = f"Отключено {count} подписок; удалённое отключение ожидается для {pending_remote} ключей"
    return RedirectResponse(f"/admin/bulk?flash={flash}", status_code=303)


@router.post("/notify")
async def bulk_notify(
    request: Request,
    message_text: str = Form(...),
    user_ids: str = Form(default=""),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    parsed_ids = _parse_user_ids(user_ids)

    if parsed_ids:
        result = await session.execute(
            select(User).where(User.id.in_(parsed_ids))
        )
        users = result.scalars().all()
    else:
        # All users with active subscriptions
        sub_result = await session.execute(
            select(Subscription.user_id).where(
                Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon])
            ).distinct()
        )
        active_user_ids = [row[0] for row in sub_result.all()]
        result = await session.execute(
            select(User).where(User.id.in_(active_user_ids))
        )
        users = result.scalars().all()

    from aiogram import Bot
    from app.config import settings

    sent = 0
    failed = 0
    bot = Bot(token=settings.BOT_TOKEN)
    try:
        for user in users:
            if user.telegram_id < 0:
                # Skip synthetic web-portal users (negative telegram_id)
                continue
            try:
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=message_text[:_TG_MESSAGE_MAX_LEN],
                    parse_mode="HTML",
                )
                sent += 1
                await asyncio.sleep(_SEND_DELAY)
            except Exception as exc:
                logger.warning("bulk_notify: failed to send to user %d (tg=%d): %s", user.id, user.telegram_id, exc)
                failed += 1
    finally:
        await bot.session.close()

    log = AdminLog(
        admin_user_id=admin.id,
        action=AdminAction.bulk_notify_users,
        details={
            "user_ids": parsed_ids or "all_active",
            "sent": sent,
            "failed": failed,
            "message_preview": message_text[:_MESSAGE_PREVIEW_LEN],
        },
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()

    flash = f"Отправлено {sent} сообщений (ошибок: {failed})"
    return RedirectResponse(f"/admin/bulk?flash={flash}", status_code=303)
