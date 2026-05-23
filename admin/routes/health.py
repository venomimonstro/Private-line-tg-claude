from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.models.health_check import SystemHealthCheck
from app.models.user import User

router = APIRouter(prefix="/health")
templates = Jinja2Templates(directory="admin/templates")
logger = logging.getLogger(__name__)


@router.get("", response_class=HTMLResponse)
async def health_page(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    try:
        result = await session.execute(
            select(SystemHealthCheck).order_by(SystemHealthCheck.checked_at.desc()).limit(10)
        )
        checks = result.scalars().all()
    except Exception:
        logger.exception("Failed to load health checks")
        checks = []
    latest = checks[0] if checks else None
    error = request.query_params.get("error", "")
    return templates.TemplateResponse("health.html", {
        "request": request, "admin": admin, "checks": checks, "latest": latest,
        "page_title": "Состояние системы", "error": error,
    })


@router.post("/run", response_model=None)
async def trigger_health_check(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    try:
        from health_monitor.checker import run_health_check
        await run_health_check(session)
        return RedirectResponse("/admin/health", status_code=303)
    except Exception as exc:
        logger.exception("Health check failed")
        from urllib.parse import quote
        return RedirectResponse(
            f"/admin/health?error={quote(str(exc)[:200])}",
            status_code=303,
        )
