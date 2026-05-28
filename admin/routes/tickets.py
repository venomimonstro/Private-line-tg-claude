from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.enums import TicketStatus
from app.models.ticket import Ticket
from app.models.ticket_message import TicketMessage
from app.models.user import User

router = APIRouter(prefix="/tickets")
templates = Jinja2Templates(directory="admin/templates")
logger = logging.getLogger(__name__)

_PAGE_SIZE = 30


@router.get("", response_class=HTMLResponse)
async def tickets_list(
    request: Request,
    status_filter: str = "",
    page: int = 1,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    base_q = select(Ticket)
    if status_filter:
        base_q = base_q.where(Ticket.status == status_filter)

    total_result = await session.execute(select(func.count()).select_from(base_q.subquery()))
    total = total_result.scalar_one()

    q = (
        base_q
        .options(selectinload(Ticket.user), selectinload(Ticket.messages))
        .order_by(Ticket.updated_at.desc())
        .offset((page - 1) * _PAGE_SIZE)
        .limit(_PAGE_SIZE)
    )
    result = await session.execute(q)
    tickets = result.scalars().all()

    return templates.TemplateResponse("tickets_list.html", {
        "request": request,
        "page_title": "Тикеты поддержки",
        "tickets": tickets,
        "total": total,
        "page": page,
        "page_size": _PAGE_SIZE,
        "status_filter": status_filter,
        "statuses": list(TicketStatus),
    })


@router.get("/{ticket_id}", response_class=HTMLResponse)
async def ticket_detail(
    ticket_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    result = await session.execute(
        select(Ticket)
        .options(selectinload(Ticket.user), selectinload(Ticket.messages))
        .where(Ticket.id == ticket_id)
    )
    ticket = result.scalar_one_or_none()
    if ticket is None:
        return RedirectResponse("/admin/tickets", status_code=303)

    return templates.TemplateResponse("ticket_detail.html", {
        "request": request,
        "page_title": f"Тикет #{ticket_id}",
        "ticket": ticket,
        "statuses": list(TicketStatus),
    })


@router.post("/{ticket_id}/reply", response_model=None)
async def ticket_reply(
    ticket_id: int,
    request: Request,
    reply_text: str = Form(...),
    new_status: str = Form(default=""),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    result = await session.execute(
        select(Ticket).options(selectinload(Ticket.user)).where(Ticket.id == ticket_id)
    )
    ticket = result.scalar_one_or_none()
    if ticket is None:
        return RedirectResponse("/admin/tickets", status_code=303)

    text = reply_text.strip()
    if text:
        now = datetime.utcnow()
        msg = TicketMessage(
            ticket_id=ticket_id,
            author_type="admin",
            text=text,
            created_at=now,
        )
        session.add(msg)

        if new_status and new_status in [s.value for s in TicketStatus]:
            ticket.status = TicketStatus(new_status)
        elif ticket.status == TicketStatus.open:
            ticket.status = TicketStatus.answered
        ticket.updated_at = now
        await session.commit()

        # Send Telegram notification to user
        if ticket.user and ticket.user.telegram_id and ticket.user.telegram_id > 0:
            try:
                from app.services.notification import notify_user
                await notify_user(
                    ticket.user.telegram_id,
                    f"💬 <b>Ответ по тикету #{ticket_id}</b>\n\n{text}",
                )
            except Exception as exc:
                logger.error("Failed to notify user for ticket %d: %s", ticket_id, exc)

    return RedirectResponse(f"/admin/tickets/{ticket_id}", status_code=303)


@router.post("/{ticket_id}/close", response_model=None)
async def ticket_close(
    ticket_id: int,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> RedirectResponse:
    ticket = await session.get(Ticket, ticket_id)
    if ticket:
        ticket.status = TicketStatus.closed
        ticket.updated_at = datetime.utcnow()
        await session.commit()
    return RedirectResponse(f"/admin/tickets/{ticket_id}", status_code=303)
