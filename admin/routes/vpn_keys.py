from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.enums import VpnKeyStatus
from app.models.admin_log import AdminLog
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.models.user import User
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services import vpn_key as vpn_key_service
from app.services.vpn_key import get_access_url

router = APIRouter(prefix="/vpn-keys")
templates = Jinja2Templates(directory="admin/templates")

_PAGE_SIZE = 25


@router.get("", response_class=HTMLResponse)
async def list_vpn_keys(
    request: Request,
    page: int = 1,
    status_filter: str = "active",
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    query = select(VpnKey)
    if status_filter:
        try:
            query = query.where(VpnKey.status == VpnKeyStatus(status_filter))
        except ValueError:
            pass

    total_result = await session.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar_one() or 0
    offset = (page - 1) * _PAGE_SIZE
    result = await session.execute(
        query.order_by(VpnKey.created_at.desc()).offset(offset).limit(_PAGE_SIZE)
    )
    keys = result.scalars().all()

    return templates.TemplateResponse("vpn_keys_list.html", {
        "request": request, "admin": admin, "keys": keys,
        "page": page, "total": total, "page_size": _PAGE_SIZE,
        "status_filter": status_filter,
        "page_title": "VPN-ключи",
    })


@router.post("/{key_id}/disable")
async def disable_key(
    key_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(select(VpnKey).where(VpnKey.id == key_id))
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404)

    server_result = await session.execute(select(VpnServer).where(VpnServer.id == key.server_id))
    server = server_result.scalar_one_or_none()
    if server:
        await vpn_key_service.disable_vpn_key(key, server, session)

    log = AdminLog(
        admin_user_id=admin.id,
        action="disable_vpn_key",
        target_entity="vpn_key",
        target_entity_id=key_id,
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/vpn-keys", status_code=303)


@router.post("/{key_id}/reissue")
async def reissue_key(
    key_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(select(VpnKey).where(VpnKey.id == key_id))
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404)

    sub_result = await session.execute(select(Subscription).where(Subscription.id == key.subscription_id))
    sub = sub_result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404)

    tariff = await session.get(Tariff, sub.tariff_id)
    if tariff is None:
        raise HTTPException(status_code=404, detail="Tariff not found")

    server_result = await session.execute(select(VpnServer).where(VpnServer.id == key.server_id))
    server = server_result.scalar_one_or_none()
    if server is None:
        raise HTTPException(status_code=404)

    try:
        await vpn_key_service.reissue_vpn_key(key, server, tariff.slug, tariff.traffic_limit_gb, session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    log = AdminLog(
        admin_user_id=admin.id,
        action="reissue_vpn_key",
        target_entity="vpn_key",
        target_entity_id=key_id,
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/vpn-keys", status_code=303)
