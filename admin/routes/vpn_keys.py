from __future__ import annotations

import json as _json
import re as _re
from urllib.parse import parse_qs as _parse_qs, urlparse as _urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.encryption import decrypt, encrypt
from app.enums import VpnKeyStatus, VpnServerStatus
from app.models.admin_log import AdminLog
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.models.user import User
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services import vpn_key as vpn_key_service
from app.services.vpn_key import get_access_url, repair_marzban_keys
from app.vpn.marzban import MarzbanClient

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


@router.post("/repair-marzban")
async def repair_marzban_endpoint(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    """Fix all active Marzban keys: update inbounds and refresh stored access URLs."""
    import logging as _logging
    from urllib.parse import quote as _quote
    _log = _logging.getLogger(__name__)
    try:
        fixed, errors, changed = await repair_marzban_keys(session)
        flash = f"Исправлено ключей: {fixed} | Ошибок: {errors} | Изменилось URL: {len(changed)}"
    except Exception as exc:
        _log.error("Admin repair_marzban failed: %s", exc, exc_info=True)
        flash = f"Ошибка: {exc}"

    log = AdminLog(
        admin_user_id=admin.id,
        action="repair_marzban_keys",
        target_entity="vpn_key",
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse(f"/admin/vpn-keys?flash={_quote(flash)}", status_code=303)


@router.post("/repair-and-notify")
async def repair_and_notify_endpoint(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> JSONResponse:
    """Run full Marzban repair then push the updated key to every affected user via bot."""
    import logging as _logging
    _log = _logging.getLogger(__name__)

    try:
        fixed, errors, changed = await repair_marzban_keys(session)
    except Exception as exc:
        _log.error("repair-and-notify: repair failed: %s", exc, exc_info=True)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    log = AdminLog(
        admin_user_id=admin.id,
        action="repair_and_notify_keys",
        target_entity="vpn_key",
        details={"fixed": fixed, "errors": errors, "changed": len(changed)},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()

    # Send updated keys to users whose URL actually changed
    from app.bot_instance import get_bot
    bot = get_bot()
    notified = 0
    notify_errors = 0
    if bot is None:
        # Admin container — create a temporary bot instance
        from aiogram import Bot
        from app.config import settings
        tmp_bot = Bot(token=settings.BOT_TOKEN)
        use_bot = tmp_bot
        close_tmp = True
    else:
        use_bot = bot
        close_tmp = False

    try:
        for telegram_id, new_url in changed:
            try:
                await use_bot.send_message(
                    chat_id=telegram_id,
                    text=(
                        "🔄 <b>Ваш VPN-ключ был обновлён</b>\n\n"
                        "🔑 <b>Ваш актуальный ключ доступа:</b>\n\n"
                        f"<code>{new_url}</code>\n\n"
                        "Удалите старый ключ из приложения и добавьте этот:\n"
                        "• <b>Android:</b> Hiddify, V2RayNG\n"
                        "• <b>iPhone:</b> Streisand, FoXray, V2Box\n"
                        "• <b>Windows/macOS:</b> Hiddify\n"
                        "• <b>Linux:</b> Hiddify, v2rayA"
                    ),
                    parse_mode="HTML",
                )
                notified += 1
            except Exception as exc:
                _log.warning("repair-and-notify: failed to message tg=%d: %s", telegram_id, exc)
                notify_errors += 1
    finally:
        if close_tmp:
            try:
                await tmp_bot.session.close()
            except Exception:
                pass

    return JSONResponse({
        "ok": True,
        "fixed": fixed,
        "errors": errors,
        "changed": len(changed),
        "notified": notified,
        "notify_errors": notify_errors,
        "message": (
            f"Repair done: {fixed} keys fixed, {errors} errors. "
            f"URLs changed: {len(changed)}. "
            f"Users notified: {notified} ({notify_errors} failed)."
        ),
    })


# ---------------------------------------------------------------------------
# Diagnostic helpers
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    """Minimal HTML escaping for inline content."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _extract_pbk(url: str) -> str:
    """Extract pbk query param from a VLESS URL; returns '' if absent."""
    try:
        parsed = _urlparse(url)
        qs = _parse_qs(parsed.query)
        vals = qs.get("pbk", [])
        return vals[0] if vals else ""
    except Exception:
        return ""


def _make_marzban_client(server: VpnServer) -> MarzbanClient:
    creds = _json.loads(decrypt(server.api_secret_encrypted))
    return MarzbanClient(server.api_url, creds["username"], creds["password"])


# ---------------------------------------------------------------------------
# GET /vpn-keys/diagnose
# ---------------------------------------------------------------------------

@router.get("/diagnose", response_class=HTMLResponse)
async def diagnose_keys(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    """Comprehensive diagnostic page for Marzban VPN key health."""
    from app.services.vpn_key import _server_protocol

    # ── Fetch all active servers ───────────────────────────────────────────
    servers_result = await session.execute(
        select(VpnServer).where(VpnServer.status == VpnServerStatus.active)
    )
    all_servers = list(servers_result.scalars().all())
    marzban_servers = [s for s in all_servers if _server_protocol(s) == "marzban"]

    # ── Fetch all active Marzban VPN keys ─────────────────────────────────
    marzban_server_ids = [s.id for s in marzban_servers]
    keys_list: list[VpnKey] = []
    if marzban_server_ids:
        keys_result = await session.execute(
            select(VpnKey).where(
                VpnKey.server_id.in_(marzban_server_ids),
                VpnKey.status == VpnKeyStatus.active,
            ).order_by(VpnKey.id)
        )
        keys_list = list(keys_result.scalars().all())

    # ── Per-server diagnostics ─────────────────────────────────────────────
    server_diags: list[dict] = []
    for server in marzban_servers:
        diag: dict = {
            "server": server,
            "auth_ok": False,
            "auth_error": None,
            "core_config_raw": None,
            "vless_inbounds": [],
            "vless_port": None,
            "reality_public_key": None,
            "reality_short_ids_count": None,
            "reality_sni": None,
            "inbounds_api_raw": None,
            "test_user": None,
            "tcp_443_ok": None,   # True/False/None
            "tcp_443_error": None,
        }
        client: MarzbanClient | None = None
        try:
            client = _make_marzban_client(server)

            # 0. TCP connect test to VPN port (443) — checks if XRay is up
            import asyncio as _asyncio
            import re as _re_host
            _m = _re_host.match(r'https?://([^:/]+)', server.api_url)
            _vpn_host = _m.group(1) if _m else None
            if _vpn_host:
                try:
                    _conn = await _asyncio.wait_for(
                        _asyncio.open_connection(_vpn_host, 443),
                        timeout=5.0,
                    )
                    _conn[1].close()
                    diag["tcp_443_ok"] = True
                except Exception as tcp_exc:
                    diag["tcp_443_ok"] = False
                    diag["tcp_443_error"] = str(tcp_exc)

            # 1. Test auth
            try:
                await client._auth()
                diag["auth_ok"] = True
            except Exception as exc:
                diag["auth_error"] = str(exc)
                server_diags.append(diag)
                continue

            # 2. Get XRay core config
            core_cfg = await client._get_core_config()
            if core_cfg is not None:
                diag["core_config_raw"] = _json.dumps(core_cfg, ensure_ascii=False, indent=2)[:4000]

                # Extract VLESS inbound info
                for inb in core_cfg.get("inbounds", []):
                    if inb.get("protocol") == "vless":
                        tag = inb.get("tag", "")
                        port = inb.get("port")
                        if diag["vless_port"] is None and isinstance(port, int):
                            diag["vless_port"] = port
                        diag["vless_inbounds"].append({"tag": tag, "port": port})

                        # Extract Reality params from this inbound
                        stream = inb.get("streamSettings", {})
                        if stream.get("security") == "reality":
                            reality_cfg = stream.get("realitySettings", {})
                            short_ids = reality_cfg.get("shortIds", [])
                            server_names = reality_cfg.get("serverNames", [])
                            diag["reality_short_ids_count"] = len(short_ids)
                            diag["reality_sni"] = server_names[0] if server_names else None

            # 3. Derive Reality public key via get_reality_params()
            reality_params = await client.get_reality_params()
            if reality_params:
                pub_key, short_id, sni = reality_params
                diag["reality_public_key"] = pub_key
                if diag["reality_sni"] is None:
                    diag["reality_sni"] = sni

            # 4. Call GET /api/inbounds and capture raw response
            try:
                headers = await client._headers()
                inb_resp = await client._client.get(
                    f"{client._base}/api/inbounds", headers=headers
                )
                diag["inbounds_api_raw"] = (
                    f"HTTP {inb_resp.status_code}: "
                    + inb_resp.text[:1000]
                )
            except Exception as exc:
                diag["inbounds_api_raw"] = f"Error: {exc}"

            # 5. Get a test user (first active Marzban user) with their links
            try:
                headers = await client._headers()
                users_resp = await client._client.get(
                    f"{client._base}/api/users",
                    headers=headers,
                    params={"limit": 1, "status": "active"},
                )
                if users_resp.status_code == 200:
                    users_data = users_resp.json()
                    user_list = users_data.get("users", [])
                    if user_list:
                        u = user_list[0]
                        diag["test_user"] = {
                            "username": u.get("username"),
                            "status": u.get("status"),
                            "inbounds": u.get("inbounds", {}),
                            "links": u.get("links", []),
                        }
                else:
                    diag["test_user"] = {"error": f"HTTP {users_resp.status_code}: {users_resp.text[:200]}"}
            except Exception as exc:
                diag["test_user"] = {"error": str(exc)}

        except Exception as exc:
            diag["auth_error"] = f"Client init error: {exc}"
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    pass
        server_diags.append(diag)

    # ── Build server_id → reality_public_key lookup ───────────────────────
    server_pubkey: dict[int, str] = {}
    for d in server_diags:
        srv = d["server"]
        if d.get("reality_public_key"):
            server_pubkey[srv.id] = d["reality_public_key"]

    # ── Analyse stored VPN keys ────────────────────────────────────────────
    key_diags: list[dict] = []
    for key in keys_list:
        try:
            url = decrypt(key.access_url_encrypted)
        except Exception as exc:
            url = f"DECRYPT_ERROR: {exc}"

        pbk_val = _extract_pbk(url)
        server_pbk = server_pubkey.get(key.server_id, "")

        key_diags.append({
            "key": key,
            "url_full": url,
            "url_preview": url[:80],
            "is_vless": url.startswith("vless://"),
            "has_reality": "security=reality" in url,
            "has_pbk": "pbk=" in url,
            "has_flow": "flow=xtls-rprx-vision" in url,
            "pbk_preview": pbk_val[:12] if pbk_val else "",
            "pbk_full": pbk_val,
            "server_pbk_preview": server_pbk[:12] if server_pbk else "",
            "pbk_matches_server": bool(pbk_val and server_pbk and pbk_val == server_pbk),
        })

    # ── Render HTML ────────────────────────────────────────────────────────
    rows_server = ""
    for d in server_diags:
        srv = d["server"]
        auth_cell = (
            '<span style="color:green">OK</span>'
            if d["auth_ok"]
            else f'<span style="color:red">FAIL: {_esc(d["auth_error"] or "")}</span>'
        )
        vless_tags = ", ".join(
            f'{_esc(i["tag"])}:{i["port"]}' for i in d["vless_inbounds"]
        ) or "<em>none</em>"
        pbk_display = _esc(d["reality_public_key"] or "—")
        short_ids_display = str(d["reality_short_ids_count"]) if d["reality_short_ids_count"] is not None else "—"
        sni_display = _esc(d["reality_sni"] or "—")
        inb_api = _esc(d["inbounds_api_raw"] or "—")

        tcp_cell = (
            '<span style="color:green;font-weight:bold">OPEN ✓</span>'
            if d["tcp_443_ok"] is True
            else (
                f'<span style="color:red;font-weight:bold">CLOSED ✗</span>'
                f'<br><small>{_esc(d["tcp_443_error"] or "")}</small>'
                if d["tcp_443_ok"] is False
                else '<span style="color:gray">N/A</span>'
            )
        )

        test_user_html = ""
        tu = d.get("test_user")
        if tu:
            if "error" in tu:
                test_user_html = f'<span style="color:red">{_esc(tu["error"])}</span>'
            else:
                links_html = "<br>".join(
                    f'<code style="font-size:0.75em;word-break:break-all">{_esc(lnk)}</code>'
                    for lnk in tu.get("links", [])
                )
                test_user_html = (
                    f'<strong>{_esc(tu.get("username",""))}</strong> '
                    f'({_esc(tu.get("status",""))})<br>'
                    f'inbounds: {_esc(_json.dumps(tu.get("inbounds",{})))}<br>'
                    f'links:<br>{links_html or "<em>none</em>"}'
                )

        rows_server += f"""
        <tr>
          <td>{srv.id}</td>
          <td>{_esc(srv.name)}</td>
          <td>{_esc(srv.api_url)}</td>
          <td>{tcp_cell}</td>
          <td>{auth_cell}</td>
          <td style="font-size:0.8em">{vless_tags}</td>
          <td style="font-size:0.8em;word-break:break-all">{pbk_display}</td>
          <td>{short_ids_display}</td>
          <td>{sni_display}</td>
          <td style="font-size:0.75em;word-break:break-all">{inb_api}</td>
          <td style="font-size:0.75em">{test_user_html}</td>
        </tr>"""

    rows_keys = ""
    for kd in key_diags:
        key = kd["key"]
        ok_icon = lambda v: '<span style="color:green">YES</span>' if v else '<span style="color:red">NO</span>'
        pbk_match_cell = (
            '<span style="color:green">MATCH</span>'
            if kd["pbk_matches_server"]
            else (
                '<span style="color:red">MISMATCH</span>'
                if kd["pbk_full"] and server_pubkey.get(key.server_id)
                else '<span style="color:orange">UNKNOWN</span>'
            )
        )
        created = key.created_at.strftime("%Y-%m-%d %H:%M") if key.created_at else "—"
        reissued = key.last_reissued_at.strftime("%Y-%m-%d %H:%M") if key.last_reissued_at else "—"

        rows_keys += f"""
        <tr>
          <td>{key.id}</td>
          <td>{_esc(key.outline_key_id)}</td>
          <td>{key.server_id}</td>
          <td style="font-size:0.75em;word-break:break-all;max-width:320px"><code>{_esc(kd["url_full"])}</code></td>
          <td>{ok_icon(kd["is_vless"])}</td>
          <td>{ok_icon(kd["has_reality"])}</td>
          <td>{ok_icon(kd["has_pbk"])}</td>
          <td>{ok_icon(kd["has_flow"])}</td>
          <td style="font-family:monospace">{_esc(kd["pbk_preview"])}…</td>
          <td style="font-family:monospace">{_esc(kd["server_pbk_preview"])}…</td>
          <td>{pbk_match_cell}</td>
          <td>{_esc(key.status.value)}</td>
          <td>{created}</td>
          <td>{reissued}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>VPN Key Diagnostics</title>
  <style>
    body {{ font-family: monospace; font-size: 13px; margin: 16px; }}
    h1 {{ font-size: 1.4em; }}
    h2 {{ font-size: 1.1em; margin-top: 2em; border-bottom: 1px solid #ccc; }}
    table {{ border-collapse: collapse; width: 100%; margin-bottom: 1.5em; }}
    th, td {{ border: 1px solid #ccc; padding: 4px 6px; vertical-align: top; text-align: left; }}
    th {{ background: #f0f0f0; }}
    form {{ margin-top: 1em; }}
    button {{ padding: 8px 16px; background: #c0392b; color: white; border: none; cursor: pointer; font-size: 13px; }}
    button:hover {{ background: #a93226; }}
    .note {{ color: #666; font-size: 0.9em; margin-bottom: 0.5em; }}
    a {{ color: #2c6fad; }}
  </style>
</head>
<body>
<h1>VPN Key Diagnostics</h1>
<p><a href="/admin/vpn-keys">&larr; Back to VPN Keys</a></p>
<p class="note">
  This page reads data from Marzban and your DB — it does NOT modify anything.
  Use <strong>Fix Keys (Safe)</strong> below to refresh stored VLESS URLs in DB
  without touching Marzban users.
</p>

<h2>1. Marzban Server Status</h2>
<table>
  <thead>
    <tr>
      <th>ID</th><th>Name</th><th>API URL</th>
      <th style="background:#ffeeee">PORT 443 TCP</th>
      <th>Marzban Auth</th>
      <th>VLESS Inbounds (tag:port)</th>
      <th>Reality pubkey</th>
      <th>shortIds count</th>
      <th>SNI</th>
      <th>/api/inbounds response</th>
      <th>Test user (first active) — FULL LINK</th>
    </tr>
  </thead>
  <tbody>{rows_server}</tbody>
</table>

<h2>2. Active DB VPN Keys on Marzban Servers ({len(key_diags)} total)</h2>
<table>
  <thead>
    <tr>
      <th>key.id</th>
      <th>marzban_username</th>
      <th>server_id</th>
      <th style="background:#fff8dc">FULL URL IN DB</th>
      <th>vless://</th>
      <th>security=reality</th>
      <th>pbk=</th>
      <th>flow=xtls-rprx-vision</th>
      <th>pbk (DB, 12ch)</th>
      <th>pbk (server, 12ch)</th>
      <th>pbk match?</th>
      <th>status</th>
      <th>created_at</th>
      <th>last_reissued_at</th>
    </tr>
  </thead>
  <tbody>{rows_keys or '<tr><td colspan="14"><em>No active Marzban keys found in DB</em></td></tr>'}</tbody>
</table>

<h2>3. Fix Keys (Safe — DB update only, no Marzban writes)</h2>
<p class="note">
  Fetches current user data from Marzban + current Reality params, builds a
  fresh VLESS URL, and updates <code>access_url_encrypted</code> in DB.
  Does NOT call PUT/POST on Marzban — safe to run at any time.
</p>
<form method="post" action="/admin/vpn-keys/diagnose/fix-keys"
      onsubmit="return confirm('Update stored VLESS URLs for all active Marzban keys?')">
  <button type="submit">Fix Keys (Safe)</button>
</form>

<h2>4. Send Keys to All Users Now</h2>
<p class="note">
  Runs the full repair (updates Marzban inbounds + DB URLs) and then immediately
  sends each user their current key via the Telegram bot.
  Use this after deploying a fix to make sure all users get their working key.
</p>
<form method="post" action="/admin/vpn-keys/repair-and-notify"
      onsubmit="return confirm('Run repair AND send keys to ALL active users via bot? This will message every user.')">
  <button type="submit" style="background:#1e6f2a">Repair + Send Keys to All Users</button>
</form>
</body>
</html>"""

    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# POST /vpn-keys/diagnose/fix-keys
# ---------------------------------------------------------------------------

@router.post("/diagnose/fix-keys")
async def fix_keys_safe(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> JSONResponse:
    """Refresh stored VLESS URLs from current Marzban state — no Marzban writes.

    For each active Marzban VPN key:
      1. GET /api/user/{username} to fetch current UUID from Marzban
      2. GET /api/core/config to get current Reality params (pubkey, short_id, sni)
      3. Build a fresh VLESS URL and update access_url_encrypted in DB

    Does NOT call PUT/POST on Marzban — safe alternative to reissue_vpn_key().
    """
    import logging as _logging
    import re as _re2
    _log = _logging.getLogger(__name__)

    from app.services.vpn_key import _server_protocol

    # Load active Marzban servers
    servers_result = await session.execute(
        select(VpnServer).where(VpnServer.status == VpnServerStatus.active)
    )
    all_servers = list(servers_result.scalars().all())
    marzban_servers = [s for s in all_servers if _server_protocol(s) == "marzban"]
    marzban_server_ids = [s.id for s in marzban_servers]

    if not marzban_server_ids:
        return JSONResponse({"ok": False, "error": "No active Marzban servers found"})

    # Load active keys on those servers
    keys_result = await session.execute(
        select(VpnKey).where(
            VpnKey.server_id.in_(marzban_server_ids),
            VpnKey.status == VpnKeyStatus.active,
        )
    )
    keys: list[VpnKey] = list(keys_result.scalars().all())

    if not keys:
        return JSONResponse({"ok": True, "fixed": 0, "errors": 0, "message": "No active Marzban keys to fix"})

    # Build one MarzbanClient per server, reuse across keys
    server_map: dict[int, VpnServer] = {s.id: s for s in marzban_servers}
    clients: dict[int, MarzbanClient] = {}
    # Cache reality params and port per server
    reality_cache: dict[int, tuple[str, str, str] | None] = {}
    port_cache: dict[int, int] = {}

    for sid, srv in server_map.items():
        try:
            clients[sid] = _make_marzban_client(srv)
        except Exception as exc:
            _log.error("fix-keys: cannot build client for server %d: %s", sid, exc)

    fixed = 0
    errors = 0
    error_details: list[str] = []

    try:
        for key in keys:
            client = clients.get(key.server_id)
            if client is None:
                errors += 1
                error_details.append(f"key {key.id}: no client for server {key.server_id}")
                continue

            username = key.outline_key_id
            try:
                # ── 1. Fetch current user from Marzban (READ ONLY) ────────
                headers = await client._headers()
                user_resp = await client._client.get(
                    f"{client._base}/api/user/{username}", headers=headers
                )
                if user_resp.status_code == 404:
                    errors += 1
                    error_details.append(f"key {key.id} ({username}): user not found in Marzban")
                    continue
                user_resp.raise_for_status()
                user_data = user_resp.json()

                # ── 2. Get UUID from user data ────────────────────────────
                uuid = user_data.get("proxies", {}).get("vless", {}).get("id", "")
                if not uuid:
                    # Try from links
                    for lnk in user_data.get("links", []):
                        if lnk.startswith("vless://"):
                            m = _re2.match(r"vless://([^@]+)@", lnk)
                            if m:
                                uuid = m.group(1)
                                break
                if not uuid:
                    errors += 1
                    error_details.append(f"key {key.id} ({username}): no UUID in Marzban response")
                    continue

                # ── 3. Get Reality params (cached per server) ─────────────
                if key.server_id not in reality_cache:
                    reality_cache[key.server_id] = await client.get_reality_params()

                reality = reality_cache[key.server_id]
                if not reality:
                    errors += 1
                    error_details.append(
                        f"key {key.id} ({username}): no Reality params on server {key.server_id}"
                    )
                    continue
                public_key, short_id, sni = reality

                # ── 4. Get VLESS port (cached per server) ─────────────────
                if key.server_id not in port_cache:
                    port_cache[key.server_id] = await client._get_vless_port()
                port = port_cache[key.server_id]

                # ── 5. Derive host from server API URL ────────────────────
                m = _re2.match(r'https?://([^:/]+)', client._base)
                host = m.group(1) if m else client._base

                # ── 6. Build fresh VLESS URL ──────────────────────────────
                new_url = (
                    f"vless://{uuid}@{host}:{port}"
                    f"?type=tcp&security=reality"
                    f"&pbk={public_key}&sni={sni}&fp=chrome"
                    f"&flow=xtls-rprx-vision"
                )
                if short_id:
                    new_url += f"&sid={short_id}"
                new_url += f"#{username}"

                # ── 7. Update DB (only if changed) ────────────────────────
                try:
                    old_url = decrypt(key.access_url_encrypted)
                except Exception:
                    old_url = ""

                key.access_url_encrypted = encrypt(new_url)
                _log.info(
                    "fix-keys: updated URL for key %d (%s): pbk=%.8s... (was: %.40s)",
                    key.id, username, public_key, old_url,
                )
                fixed += 1

            except Exception as exc:
                _log.error("fix-keys: error for key %d (%s): %s", key.id, username, exc)
                errors += 1
                error_details.append(f"key {key.id} ({username}): {str(exc)[:120]}")

    finally:
        for client in clients.values():
            try:
                await client.aclose()
            except Exception:
                pass

    if fixed > 0:
        log = AdminLog(
            admin_user_id=admin.id,
            action="fix_keys_safe",
            target_entity="vpn_key",
            details={"fixed": fixed, "errors": errors, "error_details": error_details[:20]},
            ip_address=request.client.host if request.client else None,
        )
        session.add(log)
        await session.commit()

    return JSONResponse({
        "ok": True,
        "fixed": fixed,
        "errors": errors,
        "total": len(keys),
        "error_details": error_details,
        "message": (
            f"Fixed {fixed} keys, {errors} errors "
            f"(out of {len(keys)} active Marzban keys). "
            "DB updated — no Marzban users were modified."
        ),
    })
