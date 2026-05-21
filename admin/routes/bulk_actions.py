from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.enums import SubscriptionStatus
from app.models.admin_log import AdminLog
from app.models.subscription import Subscription
from app.models.user import User

router = APIRouter(prefix="/bulk")
templates = Jinja2Templates(directory="admin/templates")

logger = logging.getLogger(__name__)


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
        action="bulk_extend_subscriptions",
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

    result = await session.execute(
        select(Subscription).where(
            Subscription.user_id.in_(parsed_ids),
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
        )
    )
    subscriptions = result.scalars().all()

    from app.enums import VpnKeyStatus
    from app.models.vpn_key import VpnKey
    from app.models.vpn_server import VpnServer
    from app.services import vpn_key as vpn_key_service
    from sqlalchemy.orm import selectinload

    count = 0
    for sub in subscriptions:
        # Reload with vpn_keys
        sub_result = await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.vpn_keys))
            .where(Subscription.id == sub.id)
        )
        sub_loaded = sub_result.scalar_one_or_none()
        if sub_loaded is None:
            continue
        for key in sub_loaded.vpn_keys:
            if key.status == VpnKeyStatus.active:
                server_result = await session.execute(
                    select(VpnServer).where(VpnServer.id == key.server_id)
                )
                server = server_result.scalar_one_or_none()
                if server:
                    await vpn_key_service.disable_vpn_key(key, server, session)
                    server.current_users = max(0, server.current_users - 1)
        sub_loaded.status = SubscriptionStatus.disabled
        count += 1

    log = AdminLog(
        admin_user_id=admin.id,
        action="bulk_disable_subscriptions",
        details={"user_ids": parsed_ids, "count": count},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()

    flash = f"Отключено {count} подписок"
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

    from app.bot_instance import get_bot

    bot = get_bot()
    sent = 0
    failed = 0
    for user in users:
        if bot is not None:
            try:
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=message_text[:4096],
                )
                sent += 1
            except Exception as exc:
                logger.warning("bulk_notify: failed to send to user %d: %s", user.id, exc)
                failed += 1
        else:
            logger.warning("bulk_notify: bot not available, skipping user %d", user.id)
            failed += 1

    log = AdminLog(
        admin_user_id=admin.id,
        action="bulk_notify_users",
        details={
            "user_ids": parsed_ids or "all_active",
            "sent": sent,
            "failed": failed,
            "message_preview": message_text[:100],
        },
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()

    flash = f"Отправлено {sent} сообщений (ошибок: {failed})"
    return RedirectResponse(f"/admin/bulk?flash={flash}", status_code=303)
