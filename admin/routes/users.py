from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import cast, func, or_, select, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.enums import AdminAction, UserStatus, VpnKeyStatus
from app.models.admin_log import AdminLog
from app.models.payment import Payment
from app.models.subscription import Subscription
from app.models.user import User
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services import vpn_key as vpn_key_service

router = APIRouter(prefix="/users")
templates = Jinja2Templates(directory="admin/templates")

_PAGE_SIZE = 25


@router.get("", response_class=HTMLResponse)
async def list_users(
    request: Request,
    page: int = 1,
    q: str = "",
    search: str = "",  # legacy alias kept for backward compat
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    # Support both q= (new) and search= (old) parameter names
    effective_q = q or search

    query = select(User)
    if effective_q:
        query = query.where(
            or_(
                User.username.ilike(f"%{effective_q}%"),
                User.first_name.ilike(f"%{effective_q}%"),
                cast(User.telegram_id, String).contains(effective_q),
            )
        )

    total_result = await session.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar_one() or 0

    offset = (page - 1) * _PAGE_SIZE
    result = await session.execute(query.order_by(User.created_at.desc()).offset(offset).limit(_PAGE_SIZE))
    users = result.scalars().all()

    return templates.TemplateResponse("users_list.html", {
        "request": request, "admin": admin, "users": users,
        "page": page, "total": total, "page_size": _PAGE_SIZE,
        "q": effective_q, "search": effective_q,  # pass both for templates
        "page_title": "Пользователи",
    })


@router.get("/{user_id}", response_class=HTMLResponse)
async def user_detail(
    user_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    result = await session.execute(
        select(User)
        .options(
            selectinload(User.subscriptions).selectinload(Subscription.tariff),
            selectinload(User.payments).selectinload(Payment.tariff),
            selectinload(User.vpn_keys),
        )
        .where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Build events timeline (last 30 events across subscriptions, payments and admin logs)
    timeline: list[dict] = []

    # Subscriptions
    sub_result = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.created_at.desc())
        .limit(20)
    )
    for sub in sub_result.scalars().all():
        timeline.append({
            "date": sub.created_at,
            "type": "subscription",
            "description": f"Подписка #{sub.id} создана — {sub.status.value}",
        })
        if sub.expires_at:
            timeline.append({
                "date": sub.expires_at,
                "type": "subscription",
                "description": f"Подписка #{sub.id} истекает",
            })

    # Payments
    pay_result = await session.execute(
        select(Payment)
        .where(Payment.user_id == user_id)
        .order_by(Payment.created_at.desc())
        .limit(20)
    )
    for pay in pay_result.scalars().all():
        timeline.append({
            "date": pay.created_at,
            "type": "payment",
            "description": f"Платёж #{pay.id} — {pay.amount} ₽ ({pay.status.value})",
        })

    # Admin log entries
    log_result = await session.execute(
        select(AdminLog)
        .where(AdminLog.target_user_id == user_id)
        .order_by(AdminLog.created_at.desc())
        .limit(20)
    )
    for log_entry in log_result.scalars().all():
        timeline.append({
            "date": log_entry.created_at,
            "type": "admin_log",
            "description": f"Действие администратора: {log_entry.action}",
        })

    timeline.sort(key=lambda e: e["date"], reverse=True)
    timeline = timeline[:30]

    return templates.TemplateResponse("user_detail.html", {
        "request": request, "admin": admin, "user": user,
        "page_title": f"Пользователь #{user_id}",
        "timeline": timeline,
    })


@router.post("/{user_id}/block")
async def block_user(
    user_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404)

    user.status = UserStatus.blocked

    keys_result = await session.execute(
        select(VpnKey)
        .options(selectinload(VpnKey.server))
        .where(VpnKey.user_id == user_id, VpnKey.status == VpnKeyStatus.active)
    )
    for key in keys_result.scalars().all():
        if key.server:
            await vpn_key_service.disable_vpn_key(key, key.server, session)
            key.server.current_users = max(0, key.server.current_users - 1)

    log = AdminLog(
        admin_user_id=admin.id,
        action=AdminAction.block_user,
        target_user_id=user_id,
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/{user_id}/unblock")
async def unblock_user(
    user_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404)

    user.status = UserStatus.active
    log = AdminLog(
        admin_user_id=admin.id,
        action=AdminAction.unblock_user,
        target_user_id=user_id,
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)
