from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.enums import SubscriptionStatus, VpnKeyStatus
from app.models.admin_log import AdminLog
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.models.user import User
from app.models.vpn_server import VpnServer
from app.services import vpn_key as vpn_key_service
from app.services.subscription import activate_subscription, extend_subscription

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
        query.options(selectinload(Subscription.tariff))
        .order_by(Subscription.created_at.desc()).offset(offset).limit(_PAGE_SIZE)
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
        select(Subscription)
        .options(selectinload(Subscription.vpn_keys), selectinload(Subscription.tariff))
        .where(Subscription.id == sub_id)
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
    log = AdminLog(
        admin_user_id=admin.id,
        action="extend_subscription",
        target_entity="subscription",
        target_entity_id=sub_id,
        details={"days": days},
        ip_address=request.client.host if request.client else None,
    )
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

    for key in sub.vpn_keys:
        if key.status == VpnKeyStatus.active:
            server_result = await session.execute(select(VpnServer).where(VpnServer.id == key.server_id))
            server = server_result.scalar_one_or_none()
            if server:
                await vpn_key_service.disable_vpn_key(key, server, session)
                server.current_users = max(0, server.current_users - 1)

    sub.status = SubscriptionStatus.disabled
    log = AdminLog(
        admin_user_id=admin.id,
        action="disable_subscription",
        target_entity="subscription",
        target_entity_id=sub_id,
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse(f"/admin/subscriptions/{sub_id}", status_code=303)


@router.post("/{sub_id}/retry")
async def retry_activation(
    sub_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(
        select(Subscription).options(selectinload(Subscription.tariff)).where(Subscription.id == sub_id)
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404)

    tariff = sub.tariff
    if tariff is None:
        raise HTTPException(status_code=400, detail="Tariff not found")

    # Reset attempt counter so activation can proceed
    sub.activation_attempts = 0
    sub.status = SubscriptionStatus.activation_error
    await session.flush()

    from app.services.user_notifications import send_key_to_user
    vpn_key = await activate_subscription(sub, tariff, session, send_key_callback=send_key_to_user)

    log = AdminLog(
        admin_user_id=admin.id,
        action="retry_activation",
        target_entity="subscription",
        target_entity_id=sub_id,
        details={"success": vpn_key is not None},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse(f"/admin/subscriptions/{sub_id}", status_code=303)
