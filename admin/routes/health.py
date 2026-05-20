from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.models.health_check import SystemHealthCheck
from app.models.user import User
from health_monitor.checker import run_health_check

router = APIRouter(prefix="/health")
templates = Jinja2Templates(directory="admin/templates")


@router.get("", response_class=HTMLResponse)
async def health_page(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    result = await session.execute(
        select(SystemHealthCheck).order_by(SystemHealthCheck.checked_at.desc()).limit(10)
    )
    checks = result.scalars().all()
    latest = checks[0] if checks else None
    return templates.TemplateResponse("health.html", {
        "request": request, "admin": admin, "checks": checks, "latest": latest,
        "page_title": "Состояние системы",
    })


@router.post("/run")
async def trigger_health_check(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    await run_health_check(session)
    return RedirectResponse("/admin/health", status_code=303)
