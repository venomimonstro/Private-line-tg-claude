from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.models.abuse_event import AbuseEvent
from app.models.user import User

router = APIRouter(prefix="/abuse")
templates = Jinja2Templates(directory="admin/templates")

_PAGE_SIZE = 25


@router.get("", response_class=HTMLResponse)
async def abuse_list(
    request: Request,
    page: int = 1,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    total_result = await session.execute(select(func.count(AbuseEvent.id)))
    total = total_result.scalar_one() or 0
    offset = (page - 1) * _PAGE_SIZE
    result = await session.execute(
        select(AbuseEvent).order_by(AbuseEvent.created_at.desc()).offset(offset).limit(_PAGE_SIZE)
    )
    events = result.scalars().all()

    return templates.TemplateResponse("abuse_list.html", {
        "request": request, "admin": admin, "events": events,
        "page": page, "total": total, "page_size": _PAGE_SIZE, "page_title": "Abuse-журнал",
    })


@router.post("/{event_id}/comment")
async def add_comment(
    event_id: int,
    request: Request,
    comment: str = Form(...),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(select(AbuseEvent).where(AbuseEvent.id == event_id))
    event = result.scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404)
    event.admin_comment = comment
    await session.commit()
    return RedirectResponse("/admin/abuse", status_code=303)
