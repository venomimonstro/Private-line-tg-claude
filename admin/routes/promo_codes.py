from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.models.admin_log import AdminLog
from app.models.user import User

router = APIRouter(prefix="/promo-codes")
templates = Jinja2Templates(directory="admin/templates")

_PAGE_SIZE = 25

try:
    from app.models.promo_code import PromoCode
    _PROMO_CODE_AVAILABLE = True
except ImportError:
    PromoCode = None  # type: ignore[assignment,misc]
    _PROMO_CODE_AVAILABLE = False


def _coming_soon(request: Request, admin: User) -> HTMLResponse:
    return templates.TemplateResponse(
        "base.html",
        {
            "request": request,
            "admin": admin,
            "page_title": "Промокоды",
            "content_override": (
                '<div class="alert alert-info mt-3">'
                "Модуль промокодов ещё не готов — модель будет добавлена в ближайшей миграции."
                "</div>"
            ),
        },
    )


@router.get("", response_class=HTMLResponse)
async def list_promo_codes(
    request: Request,
    page: int = 1,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    if not _PROMO_CODE_AVAILABLE:
        return _coming_soon(request, admin)

    total_result = await session.execute(select(func.count()).select_from(PromoCode))
    total = total_result.scalar_one() or 0

    active_result = await session.execute(
        select(func.count()).select_from(PromoCode).where(PromoCode.is_active.is_(True))
    )
    active_count = active_result.scalar_one() or 0

    uses_result = await session.execute(select(func.coalesce(func.sum(PromoCode.uses_count), 0)))
    total_uses = uses_result.scalar_one() or 0

    offset = (page - 1) * _PAGE_SIZE
    result = await session.execute(
        select(PromoCode).order_by(PromoCode.created_at.desc()).offset(offset).limit(_PAGE_SIZE)
    )
    promo_codes = result.scalars().all()

    return templates.TemplateResponse(
        "promo_codes_list.html",
        {
            "request": request,
            "admin": admin,
            "promo_codes": promo_codes,
            "page": page,
            "total": total,
            "page_size": _PAGE_SIZE,
            "active_count": active_count,
            "total_uses": total_uses,
            "page_title": "Промокоды",
        },
    )


@router.get("/new", response_class=HTMLResponse)
async def new_promo_code_form(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    if not _PROMO_CODE_AVAILABLE:
        return _coming_soon(request, admin)

    return templates.TemplateResponse(
        "promo_code_form.html",
        {
            "request": request,
            "admin": admin,
            "promo": None,
            "page_title": "Новый промокод",
        },
    )


@router.post("/new")
async def create_promo_code(
    request: Request,
    code: str = Form(...),
    description: str = Form(default=""),
    discount_percent: float = Form(default=0.0),
    discount_fixed: float = Form(default=0.0),
    tariff_slug: str = Form(default=""),
    uses_max: int = Form(default=0),
    is_trial: bool = Form(default=False),
    trial_days: int = Form(default=0),
    trial_traffic_gb: float = Form(default=0.0),
    expires_at: str = Form(default=""),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    if not _PROMO_CODE_AVAILABLE:
        return RedirectResponse("/admin/promo-codes", status_code=303)

    from datetime import datetime

    expires_dt = None
    if expires_at:
        try:
            expires_dt = datetime.fromisoformat(expires_at)
        except ValueError:
            pass

    promo = PromoCode(
        code=code.strip().upper(),
        description=description or None,
        discount_percent=discount_percent or None,
        discount_fixed=discount_fixed or None,
        tariff_slug=tariff_slug or None,
        uses_max=uses_max or None,
        uses_count=0,
        is_trial=is_trial,
        trial_days=trial_days or None,
        trial_traffic_gb=trial_traffic_gb or None,
        is_active=True,
        expires_at=expires_dt,
    )
    session.add(promo)

    log = AdminLog(
        admin_user_id=admin.id,
        action="create_promo_code",
        target_entity="promo_code",
        details={"code": code},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/promo-codes", status_code=303)


@router.get("/{promo_id}", response_class=HTMLResponse)
async def promo_code_detail(
    promo_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    if not _PROMO_CODE_AVAILABLE:
        return _coming_soon(request, admin)

    result = await session.execute(select(PromoCode).where(PromoCode.id == promo_id))
    promo = result.scalar_one_or_none()
    if promo is None:
        raise HTTPException(status_code=404, detail="Promo code not found")

    return templates.TemplateResponse(
        "promo_code_form.html",
        {
            "request": request,
            "admin": admin,
            "promo": promo,
            "page_title": f"Промокод {promo.code}",
        },
    )


@router.post("/{promo_id}/toggle")
async def toggle_promo_code(
    promo_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    if not _PROMO_CODE_AVAILABLE:
        return RedirectResponse("/admin/promo-codes", status_code=303)

    result = await session.execute(select(PromoCode).where(PromoCode.id == promo_id))
    promo = result.scalar_one_or_none()
    if promo is None:
        raise HTTPException(status_code=404)

    promo.is_active = not promo.is_active
    log = AdminLog(
        admin_user_id=admin.id,
        action="toggle_promo_code",
        target_entity="promo_code",
        target_entity_id=promo_id,
        details={"is_active": promo.is_active},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/promo-codes", status_code=303)


@router.post("/{promo_id}/delete")
async def delete_promo_code(
    promo_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    if not _PROMO_CODE_AVAILABLE:
        return RedirectResponse("/admin/promo-codes", status_code=303)

    result = await session.execute(select(PromoCode).where(PromoCode.id == promo_id))
    promo = result.scalar_one_or_none()
    if promo is None:
        raise HTTPException(status_code=404)

    if promo.uses_count and promo.uses_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete promo code with {promo.uses_count} uses",
        )

    log = AdminLog(
        admin_user_id=admin.id,
        action="delete_promo_code",
        target_entity="promo_code",
        target_entity_id=promo_id,
        details={"code": promo.code},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.delete(promo)
    await session.commit()
    return RedirectResponse("/admin/promo-codes", status_code=303)
