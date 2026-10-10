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
from app.services.vpn_key import get_access_url
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
    key = (await session.execute(
        select(VpnKey).where(VpnKey.id == key_id).with_for_update()
    )).scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404)

    server = await session.get(VpnServer, key.server_id)
    if server is None:
        key.status = VpnKeyStatus.error
        await session.commit()
        raise HTTPException(
            status_code=409,
            detail="VPN-нода отсутствует; удалённое отключение не подтверждено.",
        )

    disabled = await vpn_key_service.disable_vpn_key(key, server, session)
    if disabled:
        server.current_users = max(0, server.current_users - 1)

    session.add(AdminLog(
        admin_user_id=admin.id,
        action="disable_vpn_key",
        target_entity="vpn_key",
        target_entity_id=key_id,
        details={"confirmed": bool(disabled)},
        ip_address=request.client.host if request.client else None,
    ))
    await session.commit()
    if not disabled:
        raise HTTPException(
            status_code=409,
            detail="Отключение на VPN-ноде не подтверждено; retry поставлен в очередь.",
        )
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


async def _reconcile_all_marzban(
    session: AsyncSession,
    *,
    apply: bool,
) -> dict[str, int]:
    """One explicit preserve-first repair path for all dedicated Marzban nodes."""
    from app.services.vpn_key import _server_protocol
    from app.services.vpn_reconcile import reconcile_marzban_server

    servers = (await session.execute(
        select(VpnServer).where(
            VpnServer.provider != "private-line-primary",
        ).order_by(VpnServer.id)
    )).scalars().all()
    servers = [s for s in servers if _server_protocol(s) == "marzban"]

    summary = {
        "servers": len(servers),
        "checked": 0,
        "users_fixed": 0,
        "urls_fixed": 0,
        "hosts_fixed": 0,
        "errors": 0,
    }
    for server in servers:
        try:
            result = await reconcile_marzban_server(session, server.id, apply=apply)
            summary["checked"] += result.checked_keys
            summary["users_fixed"] += result.repaired_users
            summary["urls_fixed"] += result.repaired_urls
            summary["hosts_fixed"] += result.repaired_hosts
            summary["errors"] += result.errors
        except Exception:
            summary["errors"] += 1
    if apply:
        await session.commit()
    else:
        await session.rollback()
    return summary


@router.post("/repair-marzban")
async def repair_marzban_endpoint(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    """Explicit preserve-first reconciliation. Never rotates customer UUIDs."""
    from urllib.parse import quote as _quote

    summary = await _reconcile_all_marzban(session, apply=True)
    session.add(AdminLog(
        admin_user_id=admin.id,
        action="reconcile_marzban_keys_safe",
        target_entity="vpn_key",
        details=summary,
        ip_address=request.client.host if request.client else None,
    ))
    await session.commit()
    flash = (
        f"Проверено: {summary['checked']} | Users: {summary['users_fixed']} | "
        f"URLs: {summary['urls_fixed']} | Hosts: {summary['hosts_fixed']} | "
        f"Ошибок: {summary['errors']}"
    )
    return RedirectResponse(
        f"/admin/vpn-keys?flash={_quote(flash)}",
        status_code=303,
    )

@router.post("/repair-and-notify")
async def repair_and_notify_endpoint(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> JSONResponse:
    """Reconcile preserve-first, then explicitly resend canonical current keys."""
    summary = await _reconcile_all_marzban(session, apply=True)
    if summary["errors"]:
        return JSONResponse(
            {"ok": False, **summary, "message": "Repair has errors; users were not messaged."},
            status_code=409,
        )

    from app.models.user import User
    from app.services.user_notifications import send_key_to_user
    keys = (await session.execute(
        select(VpnKey).where(VpnKey.status == VpnKeyStatus.active)
    )).scalars().all()
    notified = 0
    notify_errors = 0
    for key in keys:
        user = await session.get(User, key.user_id)
        if user is None or not user.telegram_id:
            notify_errors += 1
            continue
        try:
            await send_key_to_user(user.telegram_id, key)
            notified += 1
        except Exception:
            notify_errors += 1

    session.add(AdminLog(
        admin_user_id=admin.id,
        action="reconcile_and_resend_keys_safe",
        target_entity="vpn_key",
        details={**summary, "notified": notified, "notify_errors": notify_errors},
        ip_address=request.client.host if request.client else None,
    ))
    await session.commit()
    return JSONResponse({
        "ok": notify_errors == 0,
        **summary,
        "notified": notified,
        "notify_errors": notify_errors,
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
                            "links_count": len(u.get("links", [])),
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
            "url_full": "[REDACTED]",
            "url_preview": "[REDACTED]",
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
      <th>Test user (first active) — secrets redacted</th>
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
      <th style="background:#fff8dc">CLIENT URL</th>
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
    """Preserve-first reconciliation; central issued UUID remains authoritative."""
    summary = await _reconcile_all_marzban(session, apply=True)
    session.add(AdminLog(
        admin_user_id=admin.id,
        action="diagnose_reconcile_keys_safe",
        target_entity="vpn_key",
        details=summary,
        ip_address=request.client.host if request.client else None,
    ))
    await session.commit()
    return JSONResponse({
        "ok": summary["errors"] == 0,
        **summary,
        "message": "Preserve-first reconciliation completed.",
    })



# ---------------------------------------------------------------------------
# GET /vpn-keys/{key_id}/test-connection
# ---------------------------------------------------------------------------

@router.get("/{key_id}/test-connection")
async def test_key_connection(
    key_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> JSONResponse:
    """Full connectivity test for a single VPN key.

    Checks:
      1. URL decryption
      2. VLESS protocol prefix
      3. Required Reality parameters (pbk, sid, sni, flow, security)
      4. SNI cleanliness (no Markdown corruption)
      5. TCP port connectivity to the VPN server
    """
    result = await session.execute(select(VpnKey).where(VpnKey.id == key_id))
    key = result.scalar_one_or_none()
    if key is None:
        return JSONResponse({"ok": False, "error": "Key not found"}, status_code=404)

    from app.services.watchdog import check_key_health
    health = await check_key_health(key, session)

    # Determine overall status
    all_ok = (
        health["tcp_ok"] is True
        and health["params_ok"]
        and health["sni_clean"] is not False
    )

    return JSONResponse({
        "ok": all_ok,
        "key_id": key_id,
        "key_username": key.outline_key_id,
        "server_id": key.server_id,
        "host": health.get("host"),
        "port": health.get("port"),
        "tcp_ok": health["tcp_ok"],
        "params_ok": health["params_ok"],
        "sni_clean": health["sni_clean"],
        "issues": health["issues"],
        "url_preview": health["url_preview"],
        "status": "OK" if all_ok else ("WARNING" if not health["issues"] else "FAIL"),
    })
