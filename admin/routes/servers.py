from __future__ import annotations

import os
import secrets
import time
from collections import defaultdict

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

# Simple in-memory rate limiter for unauthenticated node API endpoints.
# Keyed by client IP; stores list of request timestamps within the window.
_rate_registry: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW_SEC = 60
_RATE_LIMIT = 10  # requests per window per IP


def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    _rate_registry[ip] = [t for t in _rate_registry[ip] if now - t < _RATE_WINDOW_SEC]
    if len(_rate_registry[ip]) >= _RATE_LIMIT:
        return False
    _rate_registry[ip].append(now)
    return True


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
async def get_install_script(request: Request) -> PlainTextResponse:
    if not _check_rate_limit(request.client.host if request.client else ""):
        raise HTTPException(status_code=429, detail="Too many requests")
    script_path = os.path.join(settings.APP_DIR, "install_node.sh")
    if not os.path.isfile(script_path):
        raise HTTPException(status_code=404, detail="install_node.sh not found")
    with open(script_path) as f:
        content = f.read()
    return PlainTextResponse(content, media_type="text/x-sh")


@api_router.get("/fix-reality-script", response_class=PlainTextResponse)
async def get_fix_reality_script(request: Request) -> PlainTextResponse:
    if not _check_rate_limit(request.client.host if request.client else ""):
        raise HTTPException(status_code=429, detail="Too many requests")
    script_path = os.path.join(settings.APP_DIR, "fix_reality.sh")
    if not os.path.isfile(script_path):
        raise HTTPException(status_code=404, detail="fix_reality.sh not found")
    with open(script_path) as f:
        content = f.read()
    return PlainTextResponse(content, media_type="text/x-sh")


@api_router.get("/servers/{server_id}/reality-debug")
async def reality_debug(
    server_id: int,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> dict:
    """Diagnose Reality configuration for a Marzban server."""
    import json as _json
    result = await session.execute(select(VpnServer).where(VpnServer.id == server_id))
    server = result.scalar_one_or_none()
    if server is None:
        raise HTTPException(status_code=404)

    from app.encryption import decrypt as _decrypt
    from app.services.vpn_key import _server_protocol
    if _server_protocol(server) != "marzban":
        return {"error": "Not a Marzban server"}

    from app.vpn.marzban import MarzbanClient
    creds = _json.loads(_decrypt(server.api_secret_encrypted))
    client = MarzbanClient(server.api_url, creds["username"], creds["password"])

    try:
        reality = await client.get_reality_params()
    except Exception as exc:
        await client.aclose()
        return {"error": f"Failed to connect to Marzban: {exc}", "api_url": server.api_url}

    if reality is None:
        # Try to get raw config for debugging
        try:
            headers = await client._headers()
            resp = await client._client.get(f"{client._base}/api/core/config", headers=headers)
            raw = resp.text[:2000]
        except Exception as exc:
            raw = str(exc)
        await client.aclose()
        return {
            "ok": False,
            "error": "No Reality config found in /api/core/config (no inbound with security=reality)",
            "api_url": server.api_url,
            "raw_config_snippet": raw,
        }

    public_key, short_id, sni = reality

    # Extract VPN port from server config for accurate sample URL
    _vpn_port = 443
    try:
        _cfg = await client.get_core_config() if hasattr(client, "get_core_config") else None
        if _cfg is None:
            import json as _j2
            _raw2 = (await client._client.get(
                f"{client._base}/api/core/config",
                headers=await client._headers(),
            )).json()
            _vpn_port = _raw2.get("inbounds", [{}])[0].get("port", 443)
    except Exception:
        pass

    sample_url = (
        f"vless://EXAMPLE-UUID@SERVER_IP:{_vpn_port}"
        f"?type=tcp&security=reality&flow=xtls-rprx-vision"
        f"&pbk={public_key}&sid={short_id}&sni={sni}&fp=chrome#PrivateLine"
    )

    await client.aclose()
    return {
        "ok": True,
        "api_url": server.api_url,
        "public_key": public_key,
        "short_id": short_id,
        "sni": sni,
        "flow": "xtls-rprx-vision",
        "fingerprint": "chrome",
        "sample_url": sample_url,
    }


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
        # Manual entries are staging only. A node becomes customer-selectable
        # only after the safe Reality reconciliation endpoint verifies it.
        status=VpnServerStatus.disabled,
        is_selectable=False,
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
    """Read-only production-grade node test; never promotes or repairs."""
    server = await session.get(VpnServer, server_id)
    if server is None:
        raise HTTPException(status_code=404)

    from app.services.vpn_key import _make_client, _server_protocol
    protocol = _server_protocol(server)
    if protocol != "marzban":
        client = _make_client(server)
        try:
            ok = await client.check_connection()
            return {"ok": ok, "protocol": protocol, "error": None if ok else "connection failed"}
        except Exception as exc:
            return {"ok": False, "protocol": protocol, "error": type(exc).__name__}
        finally:
            await client.aclose()

    try:
        from app.services.vpn_reconcile import reconcile_marzban_server
        result = await reconcile_marzban_server(session, server_id, apply=False)
        ok = result.errors == 0
        if ok:
            from app.services.circuit_breaker import reset_circuit
            reset_circuit(server_id)
        return {
            "ok": ok,
            "protocol": "marzban",
            "integrity_errors": result.errors,
            "active_keys_checked": result.checked_keys,
            "note": "read-only; use safe reconcile to repair/publish",
        }
    except Exception as exc:
        return {
            "ok": False,
            "protocol": "marzban",
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }


@router.post("/{server_id}/fix-vless-keys")
async def fix_vless_keys(
    server_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> dict:
    """Safely reconcile a Marzban node without rotating client credentials."""
    from app.services.vpn_key import _server_protocol
    from app.services.vpn_reconcile import reconcile_marzban_server

    server = await session.get(VpnServer, server_id)
    if server is None:
        raise HTTPException(status_code=404)
    if _server_protocol(server) != "marzban":
        return {"ok": False, "error": "Not a Marzban server"}
    try:
        result = await reconcile_marzban_server(session, server_id, apply=True)
        ok = result.errors == 0
        details = {
            "checked": result.checked_keys,
            "hosts_fixed": result.repaired_hosts,
            "users_fixed": result.repaired_users,
            "urls_fixed": result.repaired_urls,
            "errors": result.errors,
            "core_resynced": result.core_restarted,
        }
    except Exception as exc:
        ok = False
        details = {"error": f"{type(exc).__name__}: {str(exc)[:180]}"}

    log = AdminLog(
        admin_user_id=admin.id,
        action="safe_reconcile_vless",
        target_entity="vpn_server",
        target_entity_id=server_id,
        details=details,
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return {"ok": ok, **details}


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
    connection_changed = bool(api_url and api_url != server.api_url) or bool(marzban_password)
    if api_url:
        server.api_url = api_url

    if marzban_password:
        username = marzban_username or "admin"
        creds = _json.dumps({"protocol": "marzban", "username": username, "password": marzban_password})
        server.api_secret_encrypted = encrypt(creds)

    if connection_changed:
        # Credentials/endpoints are untrusted until full Reality reconciliation.
        server.status = VpnServerStatus.disabled
        server.is_selectable = False

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
    server.status = VpnServerStatus.disabled
    server.is_selectable = False
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

    from app.enums import VpnKeyStatus
    live_keys = (await session.execute(
        select(VpnKey.id).where(
            VpnKey.server_id == server_id,
            VpnKey.status.in_([VpnKeyStatus.active, VpnKeyStatus.error]),
        ).limit(5)
    )).scalars().all()
    if live_keys:
        raise HTTPException(
            status_code=409,
            detail=(
                "Сервер содержит активные/неподтверждённо отключённые ключи. "
                "Сначала мигрируйте или подтвердите отключение пользователей."
            ),
        )

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
    server.is_selectable = False
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
            # Create-before-cutover: never remove the customer's working
            # credential until the replacement VLESS profile exists and passes
            # the same client formatter used by Telegram.
            already_target = (await session.execute(
                select(VpnKey.id).where(
                    VpnKey.subscription_id == old_key.subscription_id,
                    VpnKey.server_id == target.id,
                    VpnKey.status == VpnKeyStatus.active,
                ).limit(1)
            )).scalar_one_or_none() is not None
            new_key = await create_vpn_key(
                user_id=old_key.user_id,
                subscription_id=old_key.subscription_id,
                server=target,
                traffic_limit_gb=old_key.traffic_limit_gb,
                label=old_key.label,
                session=session,
            )
            from app.services.vpn_key import get_client_access_url
            get_client_access_url(new_key)  # strict VLESS Reality validation
            if not already_target:
                target.current_users += 1
            await session.flush()

            disabled = await disable_vpn_key(old_key, old_server, session)
            if not disabled:
                raise RuntimeError("old Outline credential could not be confirmed disabled")
            old_server.current_users = max(0, old_server.current_users - 1)

            db_user = await session.get(User, old_key.user_id)
            if db_user is None or not db_user.telegram_id:
                raise RuntimeError("Telegram recipient missing for migrated VPN key")
            await send_key_to_user(db_user.telegram_id, new_key)
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
    """Idempotently register only a live, externally reachable VPN node."""
    import asyncio as _asyncio
    import ipaddress as _ipaddress
    import json as _json
    from urllib.parse import urlsplit as _urlsplit
    from app.vpn.marzban import MarzbanClient

    client_ip = request.client.host if request.client else ""
    if not _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests")
    stored_token = await _get_or_create_token(session)
    incoming_token = request.headers.get("X-Node-Token", "")
    if not incoming_token or incoming_token != stored_token:
        raise HTTPException(status_code=401, detail="Invalid registration token")

    if not body.marzban_password:
        raise HTTPException(status_code=400, detail="Only verified Marzban nodes are supported")
    try:
        parsed = _urlsplit(body.api_url)
        host = parsed.hostname or ""
        api_port = parsed.port
        ip = _ipaddress.ip_address(host)
        if parsed.scheme != "http" or not ip.is_global or not api_port:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="api_url must use a globally routable IPv4 address")

    verifier = MarzbanClient(body.api_url, body.marzban_username, body.marzban_password)
    try:
        if not await verifier.check_connection():
            raise HTTPException(status_code=400, detail="Marzban authentication failed")
        cfg = await verifier._get_core_config()
        reality = await verifier.get_reality_params()
        if not cfg or not reality:
            raise HTTPException(status_code=400, detail="VLESS Reality is not configured")
        inbounds = [
            item for item in cfg.get("inbounds", [])
            if item.get("protocol") == "vless"
            and item.get("streamSettings", {}).get("security") == "reality"
        ]
        if len(inbounds) != 1:
            raise HTTPException(status_code=400, detail="Expected exactly one VLESS Reality inbound")
        tag = inbounds[0].get("tag")
        vpn_port = inbounds[0].get("port")
        if not tag or not isinstance(vpn_port, int):
            raise HTTPException(status_code=400, detail="Invalid VLESS Reality inbound")
        hosts_resp = await verifier._client.get(
            f"{verifier._base}/api/hosts", headers=await verifier._headers()
        )
        if hosts_resp.status_code != 200 or not isinstance(hosts_resp.json(), dict):
            raise HTTPException(status_code=400, detail="Marzban hosts API unavailable")
        entries = hosts_resp.json().get(tag, [])
        host_ok = any(
            isinstance(item, dict)
            and item.get("address") == host
            and item.get("port") in (vpn_port, str(vpn_port), None, "", 0, "0")
            and item.get("is_disabled") not in (True, "true", "True")
            for item in entries
        )
        if not host_ok:
            raise HTTPException(status_code=400, detail="Marzban public host does not match node IP")
        try:
            _reader, writer = await _asyncio.wait_for(
                _asyncio.open_connection(host, vpn_port), timeout=5.0
            )
            writer.close()
            await writer.wait_closed()
        except Exception:
            raise HTTPException(status_code=400, detail="Public VLESS TCP port is unreachable")
    finally:
        await verifier.aclose()

    creds = _json.dumps({
        "protocol": "marzban",
        "username": body.marzban_username,
        "password": body.marzban_password,
    })
    existing = (await session.execute(
        select(VpnServer).where(VpnServer.api_url == body.api_url)
    )).scalars().all()
    if len(existing) > 1:
        raise HTTPException(
            status_code=409,
            detail="Duplicate server records already exist for this API URL; reconcile before registration",
        )
    if existing:
        server = existing[0]
        server.name = body.name
        server.country = body.country
        server.city = body.city or None
        server.country_code = body.country_code or None
        server.flag_emoji = body.flag_emoji or None
        server.provider = "marzban"
        server.api_secret_encrypted = encrypt(creds)
        server.max_users = max(1, body.max_users)
        server.status = VpnServerStatus.active
        # Existing published nodes stay published; a newly registered node must
        # pass the control-plane disposable VLESS smoke before publication.
        server.is_selectable = bool(server.is_selectable)
        server.failure_count = 0
        server.last_error = None
        server.last_error_at = None
        action = "updated"
    else:
        server = VpnServer(
            name=body.name,
            country=body.country,
            city=body.city or None,
            country_code=body.country_code or None,
            flag_emoji=body.flag_emoji or None,
            provider="marzban",
            api_url=body.api_url,
            api_secret_encrypted=encrypt(creds),
            max_users=max(1, body.max_users),
            status=VpnServerStatus.active,
            is_selectable=False,
        )
        session.add(server)
        action = "registered"
    await session.commit()
    await session.refresh(server)
    return {
        "ok": True,
        "server_id": server.id,
        "action": action,
        "message": (
            f"VPN node '{body.name}' structurally verified and {action}; "
            "new nodes remain staging until production VLESS smoke passes"
        ),
    }

