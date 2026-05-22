from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.models.referral import Referral
from app.models.user import User

router = APIRouter(prefix="/referrals")
templates = Jinja2Templates(directory="admin/templates")

_PAGE_SIZE = 25


@router.get("", response_class=HTMLResponse)
async def list_referrals(
    request: Request,
    page: int = 1,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    total_result = await session.execute(select(func.count(Referral.id)))
    total = total_result.scalar_one() or 0
    offset = (page - 1) * _PAGE_SIZE
    result = await session.execute(
        select(Referral)
        .options(selectinload(Referral.referrer), selectinload(Referral.invited_user))
        .order_by(Referral.created_at.desc())
        .offset(offset).limit(_PAGE_SIZE)
    )
    referrals = result.scalars().all()

    return templates.TemplateResponse("referrals_list.html", {
        "request": request, "admin": admin, "referrals": referrals,
        "page": page, "total": total, "page_size": _PAGE_SIZE,
        "total_pages": (total + _PAGE_SIZE - 1) // _PAGE_SIZE,
        "page_title": "Рефералы",
    })
