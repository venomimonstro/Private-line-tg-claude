from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.models.admin_log import AdminLog
from app.models.user import User

router = APIRouter(prefix="/logs")
templates = Jinja2Templates(directory="admin/templates")

_PAGE_SIZE = 50


@router.get("", response_class=HTMLResponse)
async def logs_list(
    request: Request,
    page: int = 1,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    total_result = await session.execute(select(func.count(AdminLog.id)))
    total = total_result.scalar_one() or 0
    offset = (page - 1) * _PAGE_SIZE
    result = await session.execute(
        select(AdminLog).order_by(AdminLog.created_at.desc()).offset(offset).limit(_PAGE_SIZE)
    )
    logs = result.scalars().all()

    return templates.TemplateResponse("logs_list.html", {
        "request": request, "admin": admin, "logs": logs,
        "page": page, "total": total, "page_size": _PAGE_SIZE,
        "total_pages": (total + _PAGE_SIZE - 1) // _PAGE_SIZE,
        "page_title": "Логи действий",
    })
