from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.enums import PaymentStatus
from app.models.admin_log import AdminLog
from app.models.payment import Payment
from app.models.user import User
from app.payments.services import cancel_payment, confirm_payment

_STATUS_LABELS: dict[str, str] = {
    "created":       "Создан",
    "pending":       "Ожидает оплаты",
    "manual_review": "На проверке",
    "paid":          "Оплачен",
    "failed":        "Ошибка",
    "cancelled":     "Отменён",
    "refunded":      "Возврат",
}

router = APIRouter(prefix="/payments")
templates = Jinja2Templates(directory="admin/templates")

_PAGE_SIZE = 25


@router.get("", response_class=HTMLResponse)
async def list_payments(
    request: Request,
    page: int = 1,
    status_filter: str = "",
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    query = select(Payment)
    if status_filter:
        try:
            query = query.where(Payment.status == PaymentStatus(status_filter))
        except ValueError:
            pass

    total_result = await session.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar_one() or 0

    offset = (page - 1) * _PAGE_SIZE
    result = await session.execute(
        query.options(selectinload(Payment.tariff))
        .order_by(Payment.created_at.desc()).offset(offset).limit(_PAGE_SIZE)
    )
    payments = result.scalars().all()

    return templates.TemplateResponse("payments_list.html", {
        "request": request, "admin": admin, "payments": payments,
        "page": page, "total": total, "page_size": _PAGE_SIZE,
        "status_filter": status_filter, "statuses": list(PaymentStatus),
        "page_title": "Платежи",
    })


@router.get("/{payment_id}", response_class=HTMLResponse)
async def payment_detail(
    payment_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    result = await session.execute(
        select(Payment).options(selectinload(Payment.tariff)).where(Payment.id == payment_id)
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=404)

    return templates.TemplateResponse("payment_detail.html", {
        "request": request, "admin": admin, "payment": payment,
        "page_title": f"Платёж #{payment_id}",
        "statuses": list(PaymentStatus),
        "status_labels": _STATUS_LABELS,
    })


@router.post("/{payment_id}/confirm")
async def confirm_payment_route(
    payment_id: int,
    request: Request,
    comment: str = Form(default=""),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(select(Payment).where(Payment.id == payment_id))
    payment = result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=404)

    if payment.status not in (PaymentStatus.pending, PaymentStatus.manual_review):
        raise HTTPException(status_code=400, detail="Payment cannot be confirmed in current state")

    if comment:
        payment.comment = comment

    await confirm_payment(payment, admin.id, session)

    log = AdminLog(
        admin_user_id=admin.id,
        action="confirm_payment",
        target_entity="payment",
        target_entity_id=payment_id,
        details={"comment": comment},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()

    return RedirectResponse(f"/admin/payments/{payment_id}", status_code=303)


@router.post("/{payment_id}/change-status")
async def change_payment_status(
    payment_id: int,
    request: Request,
    new_status: str = Form(...),
    comment: str = Form(default=""),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(select(Payment).where(Payment.id == payment_id))
    payment = result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=404)

    try:
        status = PaymentStatus(new_status)
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный статус")

    old_status = payment.status.value
    payment.status = status
    payment.updated_at = datetime.utcnow()
    if comment:
        payment.comment = comment

    log = AdminLog(
        admin_user_id=admin.id,
        action="change_payment_status",
        target_entity="payment",
        target_entity_id=payment_id,
        details={"old_status": old_status, "new_status": status.value, "comment": comment},
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse(f"/admin/payments/{payment_id}", status_code=303)


@router.post("/{payment_id}/cancel")
async def cancel_payment_route(
    payment_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(select(Payment).where(Payment.id == payment_id))
    payment = result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=404)

    await cancel_payment(payment, admin.id, session)

    log = AdminLog(
        admin_user_id=admin.id,
        action="cancel_payment",
        target_entity="payment",
        target_entity_id=payment_id,
        ip_address=request.client.host if request.client else None,
    )
    session.add(log)
    await session.commit()
    return RedirectResponse(f"/admin/payments/{payment_id}", status_code=303)
