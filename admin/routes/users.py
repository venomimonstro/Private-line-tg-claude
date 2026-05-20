from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.enums import UserStatus, VpnKeyStatus
from app.models.admin_log import AdminLog
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
    search: str = "",
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    query = select(User)
    if search:
        try:
            tg_id = int(search)
            query = query.where(User.telegram_id == tg_id)
        except ValueError:
            query = query.where(
                or_(User.username.ilike(f"%{search}%"), User.first_name.ilike(f"%{search}%"))
            )

    total_result = await session.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar_one() or 0

    offset = (page - 1) * _PAGE_SIZE
    result = await session.execute(query.order_by(User.created_at.desc()).offset(offset).limit(_PAGE_SIZE))
    users = result.scalars().all()

    return templates.TemplateResponse("users_list.html", {
        "request": request, "admin": admin, "users": users,
        "page": page, "total": total, "page_size": _PAGE_SIZE,
        "search": search, "page_title": "Пользователи",
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
        .options(selectinload(User.subscriptions), selectinload(User.payments), selectinload(User.vpn_keys))
        .where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    return templates.TemplateResponse("user_detail.html", {
        "request": request, "admin": admin, "user": user, "page_title": f"Пользователь #{user_id}",
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
        select(VpnKey).where(VpnKey.user_id == user_id, VpnKey.status == VpnKeyStatus.active)
    )
    for key in keys_result.scalars().all():
        server_result = await session.execute(select(VpnServer).where(VpnServer.id == key.server_id))
        server = server_result.scalar_one_or_none()
        if server:
            await vpn_key_service.disable_vpn_key(key, server, session)

    log = AdminLog(admin_id=admin.id, action="block_user", target_type="user", target_id=user_id,
                   ip_address=request.client.host if request.client else None)
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
    log = AdminLog(admin_id=admin.id, action="unblock_user", target_type="user", target_id=user_id,
                   ip_address=request.client.host if request.client else None)
    session.add(log)
    await session.commit()
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)
