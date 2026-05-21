from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin, require_super_admin
from app.database import get_async_session
from app.models.admin_log import AdminLog
from app.models.tariff import Tariff
from app.models.user import User

router = APIRouter(prefix="/tariffs")
templates = Jinja2Templates(directory="admin/templates")


@router.get("", response_class=HTMLResponse)
async def list_tariffs(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    result = await session.execute(select(Tariff).order_by(Tariff.sort_order))
    tariffs = result.scalars().all()
    return templates.TemplateResponse("tariffs_list.html", {
        "request": request, "admin": admin, "tariffs": tariffs, "page_title": "Тарифы",
    })


@router.post("/{tariff_id}/toggle-visible")
async def toggle_visible(
    tariff_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> RedirectResponse:
    result = await session.execute(select(Tariff).where(Tariff.id == tariff_id))
    tariff = result.scalar_one_or_none()
    if tariff is None:
        raise HTTPException(status_code=404)
    tariff.is_visible = not tariff.is_visible
    log = AdminLog(
        admin_user_id=admin.id,
        action="toggle_tariff_visible",
        target_entity="tariff",
        target_entity_id=tariff_id,
        details={"is_visible": tariff.is_visible},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/tariffs", status_code=303)


@router.post("/{tariff_id}/toggle-active")
async def toggle_active(
    tariff_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> RedirectResponse:
    result = await session.execute(select(Tariff).where(Tariff.id == tariff_id))
    tariff = result.scalar_one_or_none()
    if tariff is None:
        raise HTTPException(status_code=404)
    tariff.is_active = not tariff.is_active
    log = AdminLog(
        admin_user_id=admin.id,
        action="toggle_tariff_active",
        target_entity="tariff",
        target_entity_id=tariff_id,
        details={"is_active": tariff.is_active},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/tariffs", status_code=303)
