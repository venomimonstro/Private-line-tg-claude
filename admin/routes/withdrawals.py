from __future__ import annotations

import math
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from admin.dependencies import get_current_admin, require_super_admin
from app.database import get_async_session
from app.enums import WithdrawalStatus
from app.models.user import User
from app.models.withdrawal_request import WithdrawalRequest
from app.services.notification import notify_admin

router = APIRouter(prefix="/withdrawals")
templates = Jinja2Templates(directory="admin/templates")
logger = logging.getLogger(__name__)

_PAGE_SIZE = 25


@router.get("", response_class=HTMLResponse)
async def list_withdrawals(
    request: Request,
    page: int = 1,
    status: str = "",
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    query = select(WithdrawalRequest).options(
        selectinload(WithdrawalRequest.user),
        selectinload(WithdrawalRequest.processed_by),
    )
    if status:
        query = query.where(WithdrawalRequest.status == status)

    total_result = await session.execute(
        select(func.count()).select_from(
            query.subquery()
        )
    )
    total = total_result.scalar_one() or 0
    offset = (page - 1) * _PAGE_SIZE
    result = await session.execute(
        query.order_by(WithdrawalRequest.created_at.desc()).offset(offset).limit(_PAGE_SIZE)
    )
    withdrawals = result.scalars().all()

    return templates.TemplateResponse("withdrawals_list.html", {
        "request": request,
        "admin": admin,
        "withdrawals": withdrawals,
        "page": page,
        "total": total,
        "page_size": _PAGE_SIZE,
        "total_pages": max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE),
        "status_filter": status,
        "page_title": "Запросы на вывод",
        "WithdrawalStatus": WithdrawalStatus,
    })


@router.post("/{request_id}/approve", response_model=None)
async def approve_withdrawal(
    request_id: int,
    request: Request,
    admin_note: str = Form(default=""),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> RedirectResponse:
    wr = await session.get(WithdrawalRequest, request_id, options=[selectinload(WithdrawalRequest.user)])
    if wr is None:
        raise HTTPException(status_code=404, detail="Запрос не найден")
    if wr.status != WithdrawalStatus.pending:
        raise HTTPException(status_code=400, detail="Запрос уже обработан")

    wr.status = WithdrawalStatus.approved
    wr.processed_at = datetime.utcnow()
    wr.processed_by_id = admin.id
    wr.admin_note = admin_note.strip() or None
    await session.commit()

    # Notify user via bot
    try:
        from app.bot_instance import get_bot
        bot = get_bot()
        if bot is not None and wr.user is not None:
            await bot.send_message(
                chat_id=wr.user.telegram_id,
                text=(
                    f"✅ <b>Вывод #{wr.id} одобрен!</b>\n\n"
                    f"Сумма: <b>{wr.amount_rub:.2f} ₽</b> → <b>{wr.stars_amount} ⭐ Telegram Stars</b>\n\n"
                    f"Stars будут отправлены вам в ближайшее время.\n"
                    f"После получения вы можете вывести их через <b>Fragment</b> (fragment.com)."
                    + (f"\n\nКомментарий: {wr.admin_note}" if wr.admin_note else "")
                ),
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.warning("Could not notify user about withdrawal approval: %s", exc)

    await notify_admin(
        f"✅ Вывод #{wr.id} одобрен администратором {admin.username or admin.id}\n"
        f"Пользователь TG ID: {wr.user.telegram_id if wr.user else '?'}\n"
        f"Сумма: {wr.amount_rub:.2f} ₽ → {wr.stars_amount} Stars\n"
        f"Необходимо вручную отправить Stars пользователю!"
    )

    return RedirectResponse("/admin/withdrawals", status_code=303)


@router.post("/{request_id}/reject", response_model=None)
async def reject_withdrawal(
    request_id: int,
    request: Request,
    admin_note: str = Form(default=""),
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> RedirectResponse:
    wr = await session.get(WithdrawalRequest, request_id, options=[selectinload(WithdrawalRequest.user)])
    if wr is None:
        raise HTTPException(status_code=404, detail="Запрос не найден")
    if wr.status != WithdrawalStatus.pending:
        raise HTTPException(status_code=400, detail="Запрос уже обработан")

    wr.status = WithdrawalStatus.rejected
    wr.processed_at = datetime.utcnow()
    wr.processed_by_id = admin.id
    wr.admin_note = admin_note.strip() or None

    # Refund balance back to user
    if wr.user is not None:
        from decimal import Decimal
        wr.user.referral_balance = (wr.user.referral_balance or Decimal("0")) + wr.amount_rub

    await session.commit()

    # Notify user
    try:
        from app.bot_instance import get_bot
        bot = get_bot()
        if bot is not None and wr.user is not None:
            await bot.send_message(
                chat_id=wr.user.telegram_id,
                text=(
                    f"❌ <b>Вывод #{wr.id} отклонён</b>\n\n"
                    f"Сумма {wr.amount_rub:.2f} ₽ возвращена на ваш реферальный баланс."
                    + (f"\n\nПричина: {wr.admin_note}" if wr.admin_note else "")
                ),
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.warning("Could not notify user about withdrawal rejection: %s", exc)

    return RedirectResponse("/admin/withdrawals", status_code=303)
