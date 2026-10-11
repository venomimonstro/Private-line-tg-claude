"""Explicit-consent Windows client diagnostic reports.

Payload intentionally accepts only fixed low-sensitivity fields and never raw
logs, URLs, credentials, UUIDs, IP addresses, hostnames or profile contents.
"""
from __future__ import annotations

import hmac
import json
import secrets
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select, delete

from admin.dependencies import require_super_admin
from app.database import get_async_session
from app.models.app_setting import AppSetting
from app.models.user import User
from app.models.subscription import Subscription
from app.models.vpn_key import VpnKey
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()
templates = Jinja2Templates(directory="admin/templates")
TOKEN_SETTING = "windows_diagnostics_token"
MAX_AGE = timedelta(days=14)
STAGES = frozenset(("install", "binary", "parse", "tcp", "socks", "https", "proxy", "disconnect"))
RESULTS = frozenset(("ok", "failed", "skipped"))


class StageResult(BaseModel):
    stage: str = Field(max_length=24)
    result: str = Field(max_length=12)
    code: str = Field(max_length=48)


class DiagnosticReport(BaseModel):
    version: str = Field(max_length=20)
    windows: str = Field(max_length=30)
    consent: bool
    stages: list[StageResult] = Field(min_length=1, max_length=16)


def sanitize(report: DiagnosticReport) -> dict:
    if not report.consent:
        raise ValueError("Explicit consent is required")
    results = []
    for item in report.stages:
        if item.stage not in STAGES or item.result not in RESULTS:
            raise ValueError("Unsupported diagnostic field")
        if not item.code.isascii() or not all(c.isalnum() or c == "_" for c in item.code):
            raise ValueError("Diagnostic code must be an enum-like identifier")
        results.append(item.model_dump())
    return {
        "version": "".join(c for c in report.version if c.isalnum() or c in ".-_")[:20],
        "windows": "".join(c for c in report.windows if c.isalnum() or c in ".-_")[:30],
        "stages": results,
    }


async def _token(db: AsyncSession) -> str:
    setting = await db.get(AppSetting, TOKEN_SETTING)
    if not setting or not setting.value:
        token = secrets.token_urlsafe(32)
        if setting:
            setting.value = token
        else:
            db.add(AppSetting(key=TOKEN_SETTING, value=token, description="Windows diagnostic upload token"))
        await db.commit()
        return token
    return setting.value


class PairRequest(BaseModel):
    code: str = Field(min_length=35, max_length=128)


class InstallReport(BaseModel):
    pair_code: str = Field(min_length=35, max_length=128)
    code: str = Field(min_length=1, max_length=48)
    windows: str = Field(max_length=30)
    version: str = Field(max_length=20)


@router.post("/api/windows/install-report")
async def windows_install_report(body: InstallReport, db: AsyncSession = Depends(get_async_session)):
    from app.windows_enrollment import digest, PAIR_PREFIX
    row = await db.get(AppSetting, PAIR_PREFIX + digest(body.pair_code))
    if row is None:
        raise HTTPException(status_code=403, detail="Unknown or expired pairing code")
    if not body.code.isascii() or not all(ch.isalnum() or ch == "_" for ch in body.code):
        raise HTTPException(status_code=400, detail="Invalid diagnostic code")
    try:
        pair = json.loads(row.value or "{}")
        user_id = int(pair["user_id"])
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=403, detail="Invalid pairing record")
    report_id = secrets.token_hex(8)
    safe = {
        "user_id": user_id,
        "version": "".join(ch for ch in body.version if ch.isalnum() or ch in ".-_")[:20],
        "windows": "".join(ch for ch in body.windows if ch.isalnum() or ch in ".-_")[:30],
        "stages": [{"stage": "install", "result": "failed", "code": body.code}],
    }
    db.add(AppSetting(key="win_diag_" + report_id, value=json.dumps(safe, separators=(",", ":")),
                      description="Native Windows installer failure; structured only"))
    await db.commit()
    return {"ok": True, "report_id": report_id}


@router.post("/api/windows/enroll")
async def enroll_windows_device(body: PairRequest,
                                db: AsyncSession = Depends(get_async_session)):
    from app.windows_enrollment import redeem_enrollment
    device_token = await redeem_enrollment(db, body.code)
    if not device_token:
        raise HTTPException(status_code=403, detail="Pairing code expired, already used or subscription unavailable")
    return JSONResponse({"ok": True, "device_token": device_token})


async def _device_user(request: Request, db: AsyncSession) -> int:
    from app.windows_enrollment import authenticated_device
    user_id = await authenticated_device(db, request.headers.get("authorization", ""))
    if user_id is None:
        raise HTTPException(status_code=401, detail="Windows device not paired or session expired")
    return user_id


@router.get("/api/windows/profile")
async def windows_device_profile(request: Request, db: AsyncSession = Depends(get_async_session)):
    from app.windows_enrollment import active_user_link
    user_id = await _device_user(request, db)
    url = await active_user_link(db, user_id)
    if not url:
        raise HTTPException(status_code=403, detail="VPN subscription is not active")
    return JSONResponse({"ok": True, "vless_url": url},
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/api/windows/report")
async def windows_device_report(request: Request, report: DiagnosticReport,
                                db: AsyncSession = Depends(get_async_session)):
    # No IPs, credentials or raw logs are accepted. Each report belongs to a
    # paired device and is submitted only after the user opts in on the client.
    from app.windows_enrollment import authenticated_device
    user_id = await _device_user(request, db)
    try:
        safe = sanitize(report)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    report_id = secrets.token_hex(8)
    safe["user_id"] = user_id
    db.add(AppSetting(key="win_diag_" + report_id,
                      value=json.dumps(safe, separators=(",", ":")),
                      description="Opt-in Windows automatic error report; no raw logs"))
    await db.commit()
    old = (await db.execute(select(AppSetting.key).where(
        AppSetting.key.like("win_diag_%")).order_by(AppSetting.updated_at.desc()).offset(100))).scalars().all()
    if old:
        await db.execute(delete(AppSetting).where(AppSetting.key.in_(old)))
        await db.commit()
    return {"ok": True, "report_id": report_id}


@router.get("/windows-client/personal")
async def download_personal_windows_client(user_id: int | None = None,
                                           admin: User = Depends(require_super_admin),
                                           db: AsyncSession = Depends(get_async_session)):
    from io import BytesIO
    from fastapi.responses import StreamingResponse
    from app.windows_enrollment import create_enrollment
    from app.windows_native_installer import build_personal_windows_exe
    try:
        base, code, pin = await create_enrollment(db, user_id if user_id is not None else admin.id)
        payload = build_personal_windows_exe(base_url=base, pairing_code=code, tls_pin=pin)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="Native Windows installer is not built on this server") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StreamingResponse(BytesIO(payload), media_type="application/vnd.microsoft.portable-executable",
        headers={"Content-Disposition": 'attachment; filename="PrivateLineSetup.exe"',
                 "Cache-Control": "private, no-store", "X-Content-Type-Options":"nosniff"})


@router.get("/windows-client/download")
async def download_windows_client(admin: User = Depends(require_super_admin)):
    raise HTTPException(
        status_code=410,
        detail="Legacy ZIP installer retired. Use /admin/windows-diagnostics and download PrivateLineSetup.exe.",
    )


@router.get("/windows-diagnostics", response_class=HTMLResponse)
async def diagnostics_page(request: Request, db: AsyncSession = Depends(get_async_session),
                           admin: User = Depends(require_super_admin)):
    token = await _token(db)
    rows = (await db.execute(
        select(AppSetting).where(AppSetting.key.like("win_diag_%"))
        .order_by(AppSetting.updated_at.desc()).limit(50)
    )).scalars().all()
    # Only super-admins can prepare personalised installers for existing
    # customers. These contain an expiring one-time pairing code, not keys.
    from app.enums import SubscriptionStatus, VpnKeyStatus
    eligible = (await db.execute(
        select(User)
        .join(Subscription, Subscription.user_id == User.id)
        .join(VpnKey, VpnKey.subscription_id == Subscription.id)
        .where(Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
               Subscription.expires_at > datetime.utcnow(),
               VpnKey.status == VpnKeyStatus.active)
        .distinct().order_by(User.id)
    )).scalars().all()
    reports = []
    for row in rows:
        try:
            reports.append({"id":row.key.removeprefix("win_diag_"), "at":row.updated_at,
                            "details":json.loads(row.value or "{}")})
        except (ValueError, TypeError):
            continue
    return templates.TemplateResponse("windows_diagnostics.html",
        {"request":request,"admin":admin,"page_title":"Диагностика Windows", "token":token,
         "endpoint":str(request.base_url).rstrip("/"), "reports":reports, "eligible_users":eligible})


@router.post("/api/windows-diagnostics", response_class=JSONResponse)
async def submit_diagnostics(request: Request, report: DiagnosticReport,
                             db: AsyncSession = Depends(get_async_session)):
    # A separate admin-generated bearer token prevents open public log ingestion.
    setting = await db.get(AppSetting, TOKEN_SETTING)
    supplied = request.headers.get("x-private-line-diagnostics-token", "")
    if not setting or not setting.value or not hmac.compare_digest(supplied, setting.value):
        raise HTTPException(status_code=401, detail="Invalid diagnostics token")
    try:
        safe = sanitize(report)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    report_id = secrets.token_hex(8)
    db.add(AppSetting(key="win_diag_" + report_id, value=json.dumps(safe, separators=(",", ":")),
                      description="Opt-in Windows diagnostics; no raw logs"))
    # Keep only the latest 50 reports; no client identity stored.
    await db.commit()
    old = (await db.execute(select(AppSetting.key).where(
        AppSetting.key.like("win_diag_%")).order_by(AppSetting.updated_at.desc()).offset(50))).scalars().all()
    if old:
        await db.execute(delete(AppSetting).where(AppSetting.key.in_(old)))
        await db.commit()
    return {"ok": True, "report_id": report_id}
