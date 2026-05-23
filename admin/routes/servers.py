from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, select, update as sa_update
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


@api_router.get("/fix-reality-script", response_class=PlainTextResponse)
async def get_fix_reality_script() -> PlainTextResponse:
    script_path = os.path.join(settings.APP_DIR, "fix_reality.sh")
    if not os.path.isfile(script_path):
        raise HTTPException(status_code=404, detail="fix_reality.sh not found")
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
    provider: str = Form(""),
    protocol: str = Form("marzban"),
    max_users: int = Form(default=200),
    marzban_api_url: str = Form(""),
    marzban_username: str = Form("admin"),
    marzban_password: str = Form(""),
    outline_api_url: str = Form(""),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> RedirectResponse:
    import json as _json

    if protocol == "marzban":
        api_url = marzban_api_url
        creds = _json.dumps({
            "protocol": "marzban",
            "username": marzban_username,
            "password": marzban_password,
        })
    else:
        api_url = outline_api_url
        creds = _json.dumps({"protocol": "outline"})

    server = VpnServer(
        name=name,
        country=country,
        provider=provider or protocol,
        api_url=api_url,
        api_secret_encrypted=encrypt(creds),
        max_users=max_users,
        status=VpnServerStatus.active,
    )
    session.add(server)
    log = AdminLog(
        admin_user_id=admin.id,
        action="add_server",
        target_entity="server",
        details={"name": name, "country": country, "protocol": protocol},
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
    import json as _json
    result = await session.execute(select(VpnServer).where(VpnServer.id == server_id))
    server = result.scalar_one_or_none()
    if server is None:
        raise HTTPException(status_code=404)

    from app.encryption import decrypt
    from app.services.vpn_key import _make_client, _server_protocol
    protocol = _server_protocol(server)

    # Decode stored credentials for diagnostics
    try:
        creds = _json.loads(decrypt(server.api_secret_encrypted))
    except Exception:
        creds = {}

    client = _make_client(server)
    try:
        ok = await client.check_connection()
        error = None
    except Exception as exc:
        ok = False
        error = str(exc)
    finally:
        await client.aclose()

    if ok:
        from app.services.circuit_breaker import reset_circuit
        reset_circuit(server_id)

    return {
        "ok": ok,
        "error": error,
        "url": server.api_url,
        "protocol": protocol,
        "username": creds.get("username", ""),
        "has_password": bool(creds.get("password")),
    }


@router.post("/{server_id}/edit")
async def edit_server(
    server_id: int,
    request: Request,
    name: str = Form(...),
    country: str = Form(...),
    city: str = Form(""),
    flag_emoji: str = Form(""),
    max_users: int = Form(200),
    api_url: str = Form(""),
    marzban_username: str = Form(""),
    marzban_password: str = Form(""),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> RedirectResponse:
    import json as _json
    result = await session.execute(select(VpnServer).where(VpnServer.id == server_id))
    server = result.scalar_one_or_none()
    if server is None:
        raise HTTPException(status_code=404)

    server.name = name
    server.country = country
    server.city = city or None
    server.flag_emoji = flag_emoji or None
    server.max_users = max_users
    if api_url:
        server.api_url = api_url

    if marzban_password:
        username = marzban_username or "admin"
        creds = _json.dumps({"protocol": "marzban", "username": username, "password": marzban_password})
        server.api_secret_encrypted = encrypt(creds)

    log = AdminLog(
        admin_user_id=admin.id,
        action="edit_server",
        target_entity="server",
        target_entity_id=server_id,
        details={"name": name, "country": country},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/servers", status_code=303)


@router.post("/{server_id}/update-creds")
async def update_server_creds(
    server_id: int,
    request: Request,
    marzban_username: str = Form("admin"),
    marzban_password: str = Form(""),
    marzban_api_url: str = Form(""),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> RedirectResponse:
    import json as _json
    result = await session.execute(select(VpnServer).where(VpnServer.id == server_id))
    server = result.scalar_one_or_none()
    if server is None:
        raise HTTPException(status_code=404)
    creds = _json.dumps({
        "protocol": "marzban",
        "username": marzban_username,
        "password": marzban_password,
    })
    server.api_secret_encrypted = encrypt(creds)
    if marzban_api_url:
        server.api_url = marzban_api_url
    await session.commit()
    return RedirectResponse("/admin/servers", status_code=303)


@router.post("/{server_id}/delete")
async def delete_server(
    server_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> RedirectResponse:
    from app.models.abuse_event import AbuseEvent
    from app.models.key_delivery_log import KeyDeliveryLog
    from app.models.pending_operation import PendingOperation
    from app.models.vpn_key import VpnKey

    result = await session.execute(select(VpnServer).where(VpnServer.id == server_id))
    server = result.scalar_one_or_none()
    if server is None:
        raise HTTPException(status_code=404)

    server_name = server.name

    # Collect key IDs for this server
    key_ids_result = await session.execute(
        select(VpnKey.id).where(VpnKey.server_id == server_id)
    )
    key_ids = [row[0] for row in key_ids_result.all()]

    if key_ids:
        # Nullify nullable FKs pointing at these keys
        await session.execute(
            sa_update(PendingOperation).where(PendingOperation.vpn_key_id.in_(key_ids)).values(vpn_key_id=None)
        )
        await session.execute(
            sa_update(AbuseEvent).where(AbuseEvent.vpn_key_id.in_(key_ids)).values(vpn_key_id=None)
        )
        await session.execute(
            sa_update(KeyDeliveryLog).where(KeyDeliveryLog.vpn_key_id.in_(key_ids)).values(vpn_key_id=None)
        )
        # Delete all keys for this server
        await session.execute(sa_delete(VpnKey).where(VpnKey.server_id == server_id))

    # Nullify pending operations that reference this server directly
    await session.execute(
        sa_update(PendingOperation).where(PendingOperation.server_id == server_id).values(server_id=None)
    )

    log = AdminLog(
        admin_user_id=admin.id,
        action="delete_server",
        target_entity="server",
        target_entity_id=server_id,
        details={"name": server_name, "keys_deleted": len(key_ids)},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.delete(server)
    await session.commit()
    return RedirectResponse("/admin/servers", status_code=303)


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


@router.post("/migrate-to-vless")
async def migrate_outline_to_vless(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> dict:
    """Migrate all active Outline keys to the first available Marzban server."""
    import logging
    logger = logging.getLogger(__name__)

    from app.enums import VpnKeyStatus
    from app.models.vpn_key import VpnKey
    from app.services.vpn_key import _server_protocol, create_vpn_key, disable_vpn_key
    from app.services.user_notifications import send_key_to_user

    servers_result = await session.execute(
        select(VpnServer).where(VpnServer.status == VpnServerStatus.active)
    )
    all_servers = list(servers_result.scalars().all())

    marzban_servers = [s for s in all_servers if _server_protocol(s) == "marzban"]
    if not marzban_servers:
        raise HTTPException(
            status_code=400,
            detail="Нет Marzban-серверов. Сначала установите ноду с помощью команды из этой страницы.",
        )
    target = marzban_servers[0]

    outline_servers = {s.id: s for s in all_servers if _server_protocol(s) == "outline"}
    if not outline_servers:
        return {"migrated": 0, "failed": 0, "message": "Outline-серверов не найдено — миграция не нужна."}

    keys_result = await session.execute(
        select(VpnKey).where(
            VpnKey.server_id.in_(list(outline_servers.keys())),
            VpnKey.status == VpnKeyStatus.active,
        )
    )
    old_keys = list(keys_result.scalars().all())

    migrated = 0
    failed = 0

    for old_key in old_keys:
        old_server = outline_servers[old_key.server_id]
        try:
            # Disable Outline key first (clears idempotency check)
            await disable_vpn_key(old_key, old_server, session)
            old_server.current_users = max(0, old_server.current_users - 1)

            # Create VLESS key on Marzban
            new_key = await create_vpn_key(
                user_id=old_key.user_id,
                subscription_id=old_key.subscription_id,
                server=target,
                traffic_limit_gb=old_key.traffic_limit_gb,
                label=old_key.label,
                session=session,
            )
            target.current_users += 1
            await session.flush()

            # Send new key to user
            await send_key_to_user(old_key.user_id, new_key)
            migrated += 1
        except Exception as exc:
            logger.error("Migration failed for key %d: %s", old_key.id, exc)
            failed += 1

    # Disable the old Outline servers so new subscriptions go to Marzban
    for s in outline_servers.values():
        s.status = VpnServerStatus.disabled

    log = AdminLog(
        admin_user_id=admin.id,
        action="migrate_outline_to_vless",
        details={"migrated": migrated, "failed": failed},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()

    return {
        "migrated": migrated,
        "failed": failed,
        "message": f"✅ Мигрировано {migrated} ключей, ошибок: {failed}. Outline-серверы отключены.",
    }


# ── Node auto-registration API ─────────────────────────────────────────────────

class NodeRegistrationRequest(BaseModel):
    name: str
    country: str
    api_url: str
    city: str = ""
    country_code: str = ""
    flag_emoji: str = ""
    provider: str = "marzban"
    # Marzban credentials
    marzban_username: str = "admin"
    marzban_password: str = ""
    # Legacy Outline fields (kept for backward compat)
    cert_sha256: str = ""
    max_users: int = 100


@api_router.post("/register-node")
async def register_node(
    request: Request,
    body: NodeRegistrationRequest,
    session: AsyncSession = Depends(get_async_session),
) -> dict:
    import json as _json
    stored_token = await _get_or_create_token(session)
    incoming_token = request.headers.get("X-Node-Token", "")
    if not incoming_token or incoming_token != stored_token:
        raise HTTPException(status_code=401, detail="Invalid registration token")

    # Build credentials (no back-check — node initiates, admin trusts the token)
    if body.marzban_password:
        creds = _json.dumps({"protocol": "marzban", "username": body.marzban_username, "password": body.marzban_password})
    else:
        creds = _json.dumps({"protocol": "outline"})

    server = VpnServer(
        name=body.name,
        country=body.country,
        city=body.city or None,
        country_code=body.country_code or None,
        flag_emoji=body.flag_emoji or None,
        provider=body.provider,
        api_url=body.api_url,
        api_secret_encrypted=encrypt(creds),
        max_users=body.max_users,
        status=VpnServerStatus.active,
    )
    session.add(server)
    await session.commit()
    await session.refresh(server)
    return {"ok": True, "server_id": server.id, "message": f"Сервер '{body.name}' зарегистрирован (ID: {server.id})"}
