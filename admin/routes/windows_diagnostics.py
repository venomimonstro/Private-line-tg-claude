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


@router.get("/admin/windows-client/download")
async def download_windows_client(admin: User = Depends(require_super_admin)):
    from io import BytesIO
    from fastapi.responses import StreamingResponse
    from app.windows_bundle import build_windows_client

    return StreamingResponse(BytesIO(build_windows_client()), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="private-line-windows.zip"',
                 "Cache-Control":"private, no-store", "X-Content-Type-Options":"nosniff"})


@router.get("/admin/windows-diagnostics", response_class=HTMLResponse)
async def diagnostics_page(request: Request, db: AsyncSession = Depends(get_async_session),
                           admin: User = Depends(require_super_admin)):
    token = await _token(db)
    rows = (await db.execute(
        select(AppSetting).where(AppSetting.key.like("win_diag_%"))
        .order_by(AppSetting.updated_at.desc()).limit(50)
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
         "endpoint":str(request.base_url).rstrip("/"), "reports":reports})


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
