from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.enums import SubscriptionStatus
from app.models.admin_log import AdminLog
from app.models.subscription import Subscription
from app.models.user import User
from app.services.subscription import extend_subscription

router = APIRouter(prefix="/subscriptions")
templates = Jinja2Templates(directory="admin/templates")

_PAGE_SIZE = 25


@router.get("", response_class=HTMLResponse)
async def list_subscriptions(
    request: Request,
    page: int = 1,
    status_filter: str = "",
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    query = select(Subscription)
    if status_filter:
        try:
            query = query.where(Subscription.status == SubscriptionStatus(status_filter))
        except ValueError:
            pass

    total_result = await session.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar_one() or 0

    offset = (page - 1) * _PAGE_SIZE
    result = await session.execute(
        query.order_by(Subscription.created_at.desc()).offset(offset).limit(_PAGE_SIZE)
    )
    subscriptions = result.scalars().all()

    return templates.TemplateResponse("subscriptions_list.html", {
        "request": request, "admin": admin, "subscriptions": subscriptions,
        "page": page, "total": total, "page_size": _PAGE_SIZE,
        "status_filter": status_filter, "statuses": list(SubscriptionStatus),
        "page_title": "Подписки",
    })


@router.get("/{sub_id}", response_class=HTMLResponse)
async def subscription_detail(
    sub_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    result = await session.execute(
        select(Subscription).options(selectinload(Subscription.vpn_keys)).where(Subscription.id == sub_id)
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("subscription_detail.html", {
        "request": request, "admin": admin, "sub": sub, "page_title": f"Подписка #{sub_id}",
    })


@router.post("/{sub_id}/extend")
async def extend_subscription_route(
    sub_id: int,
    request: Request,
    days: int = Form(...),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(select(Subscription).where(Subscription.id == sub_id))
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404)

    await extend_subscription(sub, days, session)
    log = AdminLog(admin_id=admin.id, action="extend_subscription", target_type="subscription", target_id=sub_id,
                   details={"days": days}, ip_address=request.client.host if request.client else None)
    session.add(log)
    await session.commit()
    return RedirectResponse(f"/admin/subscriptions/{sub_id}", status_code=303)


@router.post("/{sub_id}/disable")
async def disable_subscription(
    sub_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(
        select(Subscription).options(selectinload(Subscription.vpn_keys)).where(Subscription.id == sub_id)
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404)

    from app.enums import VpnKeyStatus
    from app.models.vpn_server import VpnServer
    from app.services import vpn_key as vpn_key_service
    for key in sub.vpn_keys:
        if key.status == VpnKeyStatus.active:
            server_result = await session.execute(select(VpnServer).where(VpnServer.id == key.server_id))
            server = server_result.scalar_one_or_none()
            if server:
                await vpn_key_service.disable_vpn_key(key, server, session)

    sub.status = SubscriptionStatus.disabled
    log = AdminLog(admin_id=admin.id, action="disable_subscription", target_type="subscription", target_id=sub_id,
                   ip_address=request.client.host if request.client else None)
    session.add(log)
    await session.commit()
    return RedirectResponse(f"/admin/subscriptions/{sub_id}", status_code=303)
