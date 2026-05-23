from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin, require_super_admin
from app.config import settings
from app.database import get_async_session
from app.encryption import encrypt
from app.enums import VpnServerStatus
from app.models.admin_log import AdminLog
from app.models.app_setting import AppSetting
from app.models.user import User
from app.models.vpn_server import VpnServer
from app.vpn.outline import OutlineClient

router = APIRouter(prefix="/servers")
api_router = APIRouter(prefix="/api")
templates = Jinja2Templates(directory="admin/templates")

_TOKEN_KEY = "node_register_token"


async def _get_or_create_token(session: AsyncSession) -> str:
    """Return the node registration token, auto-generating it on first call."""
    row = await session.get(AppSetting, _TOKEN_KEY)
    if row and row.value:
        return row.value
    token = secrets.token_urlsafe(32)
    if row:
        row.value = token
    else:
        session.add(AppSetting(
            key=_TOKEN_KEY,
            value=token,
            description="Token for automatic VPN node registration via install_node.sh",
        ))
    await session.commit()
    return token


@api_router.get("/install-script", response_class=PlainTextResponse)
async def get_install_script() -> PlainTextResponse:
    script_path = os.path.join(settings.APP_DIR, "install_node.sh")
    if not os.path.isfile(script_path):
        raise HTTPException(status_code=404, detail="install_node.sh not found")
    with open(script_path) as f:
        content = f.read()
    return PlainTextResponse(content, media_type="text/x-sh")


@router.get("", response_class=HTMLResponse)
async def list_servers(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    result = await session.execute(select(VpnServer).order_by(VpnServer.id))
    servers = result.scalars().all()
    token = await _get_or_create_token(session)
    return templates.TemplateResponse("servers_list.html", {
        "request": request, "admin": admin, "servers": servers, "page_title": "VPN-серверы",
        "node_register_token": token,
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


# ── Node auto-registration API ─────────────────────────────────────────────────

class NodeRegistrationRequest(BaseModel):
    name: str
    country: str
    api_url: str
    city: str = ""
    country_code: str = ""
    flag_emoji: str = ""
    provider: str = "unknown"
    cert_sha256: str = ""
    max_users: int = 100


@api_router.post("/register-node")
async def register_node(
    request: Request,
    body: NodeRegistrationRequest,
    session: AsyncSession = Depends(get_async_session),
) -> dict:
    stored_token = await _get_or_create_token(session)
    incoming_token = request.headers.get("X-Node-Token", "")
    if not incoming_token or incoming_token != stored_token:
        raise HTTPException(status_code=401, detail="Invalid registration token")

    client = OutlineClient(api_url=body.api_url)
    reachable = await client.check_connection()
    await client.aclose()
    if not reachable:
        raise HTTPException(status_code=400, detail="Cannot connect to Outline API at the provided URL")

    server = VpnServer(
        name=body.name,
        country=body.country,
        city=body.city or None,
        country_code=body.country_code or None,
        flag_emoji=body.flag_emoji or None,
        provider=body.provider,
        api_url=body.api_url,
        api_secret_encrypted=encrypt(body.cert_sha256 or body.api_url),
        max_users=body.max_users,
        status=VpnServerStatus.active,
    )
    session.add(server)
    await session.commit()
    await session.refresh(server)
    return {"ok": True, "server_id": server.id, "message": f"Сервер '{body.name}' зарегистрирован (ID: {server.id})"}
