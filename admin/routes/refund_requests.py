from __future__ import annotations
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.models.admin_log import AdminLog
from app.models.refund_request import RefundRequest
from app.models.user import User
from app.services.refund import process_refund

router = APIRouter(prefix="/refund-requests")
templates = Jinja2Templates(directory="admin/templates")
_PAGE_SIZE = 25


@router.get("", response_class=HTMLResponse)
async def list_refunds(
    request: Request,
    page: int = 1,
    status_filter: str = "",
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    query = select(RefundRequest)
    if status_filter:
        query = query.where(RefundRequest.status == status_filter)

    total = (await session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    offset = (page - 1) * _PAGE_SIZE
    rows = (await session.execute(query.order_by(RefundRequest.created_at.desc()).offset(offset).limit(_PAGE_SIZE))).scalars().all()

    return templates.TemplateResponse("refund_requests_list.html", {
        "request": request, "admin": admin,
        "refunds": rows, "page": page,
        "total_pages": (total + _PAGE_SIZE - 1) // _PAGE_SIZE,
        "status_filter": status_filter,
        "page_title": "Заявки на возврат",
    })


@router.post("/{refund_id}/approve")
async def approve_refund(
    refund_id: int,
    request: Request,
    comment: str = Form(default=""),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    req = await session.get(RefundRequest, refund_id)
    if req is None:
        raise HTTPException(404)
    await process_refund(refund_id, admin.id, approve=True, admin_comment=comment, session=session)
    log = AdminLog(admin_user_id=admin.id, action="approve_refund",
                   target_entity="refund_request", target_entity_id=refund_id,
                   ip_address=request.client.host if request.client else None)
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/refund-requests", status_code=303)


@router.post("/{refund_id}/deny")
async def deny_refund(
    refund_id: int,
    request: Request,
    comment: str = Form(default=""),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    req = await session.get(RefundRequest, refund_id)
    if req is None:
        raise HTTPException(404)
    await process_refund(refund_id, admin.id, approve=False, admin_comment=comment, session=session)
    log = AdminLog(admin_user_id=admin.id, action="deny_refund",
                   target_entity="refund_request", target_entity_id=refund_id,
                   ip_address=request.client.host if request.client else None)
    session.add(log)
    await session.commit()
    return RedirectResponse("/admin/refund-requests", status_code=303)
