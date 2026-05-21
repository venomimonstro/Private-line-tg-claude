from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin, require_super_admin
from app.database import get_async_session
from app.encryption import encrypt
from app.enums import VpnServerStatus
from app.models.admin_log import AdminLog
from app.models.user import User
from app.models.vpn_server import VpnServer
from app.vpn.outline import OutlineClient

router = APIRouter(prefix="/servers")
templates = Jinja2Templates(directory="admin/templates")


@router.get("", response_class=HTMLResponse)
async def list_servers(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    result = await session.execute(select(VpnServer).order_by(VpnServer.id))
    servers = result.scalars().all()
    return templates.TemplateResponse("servers_list.html", {
        "request": request, "admin": admin, "servers": servers, "page_title": "VPN-серверы",
    })


@router.post("", response_class=RedirectResponse)
async def add_server(
    request: Request,
    name: str = Form(...),
    country: str = Form(...),
    provider: str = Form(...),
    api_url: str = Form(...),
    api_secret: str = Form(...),
    max_users: int = Form(default=100),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> RedirectResponse:
    server = VpnServer(
        name=name,
        country=country,
        provider=provider,
        api_url=api_url,
        api_secret_encrypted=encrypt(api_secret),
        max_users=max_users,
        status=VpnServerStatus.active,
    )
    session.add(server)
    log = AdminLog(
        admin_user_id=admin.id,
        action="add_server",
        target_entity="server",
        details={"name": name, "country": country},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/servers", status_code=303)


@router.post("/{server_id}/test")
async def test_server(
    server_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> dict:
    result = await session.execute(select(VpnServer).where(VpnServer.id == server_id))
    server = result.scalar_one_or_none()
    if server is None:
        raise HTTPException(status_code=404)

    client = OutlineClient(api_url=server.api_url)
    ok = await client.check_connection()
    await client.aclose()
    return {"ok": ok}


@router.post("/{server_id}/disable")
async def disable_server(
    server_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> RedirectResponse:
    result = await session.execute(select(VpnServer).where(VpnServer.id == server_id))
    server = result.scalar_one_or_none()
    if server is None:
        raise HTTPException(status_code=404)
    server.status = VpnServerStatus.disabled
    log = AdminLog(
        admin_user_id=admin.id,
        action="disable_server",
        target_entity="server",
        target_entity_id=server_id,
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/servers", status_code=303)
