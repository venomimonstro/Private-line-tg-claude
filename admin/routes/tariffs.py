from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin, require_super_admin
from app.database import get_async_session
from app.enums import AdminAction, SubscriptionStatus
from app.models.admin_log import AdminLog
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.models.user import User

router = APIRouter(prefix="/tariffs")
templates = Jinja2Templates(directory="admin/templates")

_INACTIVE_STATUSES = {SubscriptionStatus.expired, SubscriptionStatus.disabled, SubscriptionStatus.blocked}


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


@router.get("/new", response_class=HTMLResponse)
async def new_tariff_form(
    request: Request,
    admin: User = Depends(require_super_admin),
) -> HTMLResponse:
    return templates.TemplateResponse("tariff_form.html", {
        "request": request,
        "admin": admin,
        "tariff": None,
        "page_title": "Новый тариф",
        "error": None,
    })


def _form_error(request, admin, error: str) -> HTMLResponse:
    return templates.TemplateResponse("tariff_form.html", {
        "request": request, "admin": admin, "tariff": None,
        "page_title": "Новый тариф", "error": error,
    })


def _parse_int_field(value: str, field_name: str) -> int:
    v = value.strip()
    if not v:
        raise ValueError(f"Поле «{field_name}» обязательно")
    try:
        return int(v)
    except (ValueError, TypeError):
        raise ValueError(f"Поле «{field_name}» должно быть целым числом")


@router.post("/new", response_model=None)
async def create_tariff(
    request: Request,
    slug: str = Form(...),
    name: str = Form(...),
    description: str = Form(default=""),
    price_monthly: str = Form(...),
    price_first_month: str = Form(default=""),
    currency: str = Form(default="RUB"),
    duration_days: str = Form(default=""),
    traffic_limit_gb: str = Form(default=""),
    max_keys: str = Form(default="1"),
    speed_limit_mbps: str = Form(default=""),
    sort_order: str = Form(default="0"),
    is_active: bool = Form(default=False),
    is_visible: bool = Form(default=False),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> RedirectResponse | HTMLResponse:
    try:
        dur = _parse_int_field(duration_days, "Срок действия (дней)")
        keys = _parse_int_field(max_keys or "1", "Макс. ключей")
        order = int(sort_order.strip() or "0")
        price = Decimal(price_monthly.strip())
    except (ValueError, Exception) as exc:
        return _form_error(request, admin, str(exc))

    existing = await session.execute(select(Tariff).where(Tariff.slug == slug.strip()))
    if existing.scalar_one_or_none() is not None:
        return _form_error(request, admin, f"Тариф с slug '{slug}' уже существует")

    tariff = Tariff(
        slug=slug.strip(),
        name=name.strip(),
        description=description.strip() or None,
        price_monthly=price,
        price_first_month=Decimal(price_first_month.strip()) if price_first_month.strip() else None,
        currency=currency.strip() or "RUB",
        duration_days=dur,
        traffic_limit_gb=int(traffic_limit_gb.strip()) if traffic_limit_gb.strip() else None,
        max_keys=keys,
        speed_limit_mbps=int(speed_limit_mbps.strip()) if speed_limit_mbps.strip() else None,
        sort_order=order,
        is_active=is_active,
        is_visible=is_visible,
    )
    session.add(tariff)
    await session.flush()

    log = AdminLog(
        admin_user_id=admin.id,
        action=AdminAction.create_tariff,
        target_entity="tariff",
        target_entity_id=tariff.id,
        details={"slug": tariff.slug, "name": tariff.name},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/tariffs", status_code=303)


@router.get("/{tariff_id}/edit", response_class=HTMLResponse)
async def edit_tariff_form(
    tariff_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> HTMLResponse:
    result = await session.execute(select(Tariff).where(Tariff.id == tariff_id))
    tariff = result.scalar_one_or_none()
    if tariff is None:
        raise HTTPException(status_code=404, detail="Тариф не найден")
    return templates.TemplateResponse("tariff_form.html", {
        "request": request,
        "admin": admin,
        "tariff": tariff,
        "page_title": f"Редактировать тариф: {tariff.name}",
        "error": None,
    })


@router.post("/{tariff_id}/edit", response_model=None)
async def update_tariff(
    tariff_id: int,
    request: Request,
    slug: str = Form(...),
    name: str = Form(...),
    description: str = Form(default=""),
    price_monthly: str = Form(...),
    price_first_month: str = Form(default=""),
    currency: str = Form(default="RUB"),
    duration_days: str = Form(default=""),
    traffic_limit_gb: str = Form(default=""),
    max_keys: str = Form(default="1"),
    speed_limit_mbps: str = Form(default=""),
    sort_order: str = Form(default="0"),
    is_active: bool = Form(default=False),
    is_visible: bool = Form(default=False),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> RedirectResponse | HTMLResponse:
    result = await session.execute(select(Tariff).where(Tariff.id == tariff_id))
    tariff = result.scalar_one_or_none()
    if tariff is None:
        raise HTTPException(status_code=404, detail="Тариф не найден")

    def edit_error(msg: str) -> HTMLResponse:
        return templates.TemplateResponse("tariff_form.html", {
            "request": request, "admin": admin, "tariff": tariff,
            "page_title": f"Редактировать тариф: {tariff.name}", "error": msg,
        })

    try:
        dur = _parse_int_field(duration_days, "Срок действия (дней)")
        keys = _parse_int_field(max_keys or "1", "Макс. ключей")
        order = int(sort_order.strip() or "0")
        price = Decimal(price_monthly.strip())
    except (ValueError, Exception) as exc:
        return edit_error(str(exc))

    dup = await session.execute(
        select(Tariff).where(Tariff.slug == slug.strip(), Tariff.id != tariff_id)
    )
    if dup.scalar_one_or_none() is not None:
        return edit_error(f"Тариф с slug '{slug}' уже существует")

    tariff.slug = slug.strip()
    tariff.name = name.strip()
    tariff.description = description.strip() or None
    tariff.price_monthly = price
    tariff.price_first_month = Decimal(price_first_month.strip()) if price_first_month.strip() else None
    tariff.currency = currency.strip() or "RUB"
    tariff.duration_days = dur
    tariff.traffic_limit_gb = int(traffic_limit_gb.strip()) if traffic_limit_gb.strip() else None
    tariff.max_keys = keys
    tariff.speed_limit_mbps = int(speed_limit_mbps.strip()) if speed_limit_mbps.strip() else None
    tariff.sort_order = order
    tariff.is_active = is_active
    tariff.is_visible = is_visible

    log = AdminLog(
        admin_user_id=admin.id,
        action=AdminAction.update_tariff,
        target_entity="tariff",
        target_entity_id=tariff_id,
        details={"slug": tariff.slug, "name": tariff.name},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/tariffs", status_code=303)


@router.post("/{tariff_id}/delete")
async def delete_tariff(
    tariff_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> RedirectResponse:
    result = await session.execute(select(Tariff).where(Tariff.id == tariff_id))
    tariff = result.scalar_one_or_none()
    if tariff is None:
        raise HTTPException(status_code=404, detail="Тариф не найден")

    # Check for active subscriptions
    active_sub = await session.execute(
        select(Subscription).where(
            Subscription.tariff_id == tariff_id,
            Subscription.status.notin_([s.value for s in _INACTIVE_STATUSES]),
        ).limit(1)
    )
    if active_sub.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=400,
            detail="Невозможно удалить тариф: есть активные подписки",
        )

    log = AdminLog(
        admin_user_id=admin.id,
        action=AdminAction.delete_tariff,
        target_entity="tariff",
        target_entity_id=tariff_id,
        details={"slug": tariff.slug, "name": tariff.name},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.delete(tariff)
    await session.commit()
    return RedirectResponse("/admin/tariffs", status_code=303)


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
