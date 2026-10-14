"""Super-admin initiated VPN test. Never stores or logs pasted credentials."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import require_super_admin
from app.config import settings
from app.database import get_async_session
from app.enums import SubscriptionStatus, VpnKeyStatus
from app.models.subscription import Subscription
from app.models.user import User
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.vpn_key import get_client_access_url
from app.check_exact_vless import signature
from app.vpn.admin_diagnostic import _site, explain, run_diagnostic

router = APIRouter(prefix="/vpn-diagnostic")
templates = Jinja2Templates(directory="admin/templates")
_gate = asyncio.Semaphore(1)
_last_run = 0.0


def _csrf(request: Request) -> str:
    session = request.cookies.get("admin_session", "")
    return hmac.new(settings.SECRET_KEY.encode(), (session + ":vpn-diagnostic").encode(), hashlib.sha256).hexdigest()


def _page(request: Request, **values: object) -> HTMLResponse:
    response = templates.TemplateResponse("vpn_diagnostic.html", {
        "request": request, "page_title": "Проверка VPN-ключа", "csrf": _csrf(request),
        "result": None, "analysis": None, "error": None, "site": "https://www.google.com/",
        **values,
    })
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("", response_class=HTMLResponse)
async def page(request: Request, admin: User = Depends(require_super_admin)) -> HTMLResponse:
    return _page(request)


@router.post("", response_class=HTMLResponse)
async def run(request: Request, vless_url: str = Form(""), site_url: str = Form(""),
              client_log: str = Form(""), csrf: str = Form(""),
              db: AsyncSession = Depends(get_async_session),
              admin: User = Depends(require_super_admin)) -> HTMLResponse:
    global _last_run
    if not hmac.compare_digest(csrf, _csrf(request)):
        raise HTTPException(status_code=403, detail="Invalid form token")
    if (len(vless_url) > 4096 or len(client_log) > 12000 or
            len(site_url) > 1200 or not vless_url or not site_url):
        return _page(request, error="Укажите ключ и HTTPS-сайт. Лимит журнала — 12 000 символов.")
    try:
        supplied = signature(vless_url)
        _site(site_url)
    except (ValueError, TypeError):
        return _page(request, error="Некорректный VLESS-ключ или HTTPS-адрес сайта.", site=site_url)

    rows = (await db.execute(
        select(VpnKey).join(Subscription, Subscription.id == VpnKey.subscription_id)
        .join(VpnServer, VpnServer.id == VpnKey.server_id)
        .where(VpnKey.status == VpnKeyStatus.active,
               VpnServer.api_url == f"direct-xray://{supplied[1]}:{supplied[2]}",
               Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
               Subscription.expires_at > datetime.utcnow())
    )).scalars().all()
    matched = False
    for key in rows:
        try:
            if signature(get_client_access_url(key)) == supplied:
                matched = True
                break
        except Exception:
            continue
    if not matched:
        return _page(request, error="Ключ не совпадает с активным ключом оплаченной подписки.", site=site_url)

    # Avoid long queued probes under load. The admin process uses one worker.
    if _gate.locked() or time.monotonic() - _last_run < 15:
        return _page(request, error="Проверка уже идёт. Повторите через 15 секунд.", site=site_url)
    async with _gate:
        _last_run = time.monotonic()
        # Do not hold a DB transaction while waiting for network I/O.
        await db.rollback()
        results = await asyncio.to_thread(run_diagnostic, vless_url, site_url)
    return _page(request, result=results, analysis=explain(results, client_log), site=site_url)
