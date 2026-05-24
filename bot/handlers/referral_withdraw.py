from __future__ import annotations

import logging
import math
from datetime import datetime
from decimal import Decimal

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.config import settings
from app.database import async_session_maker
from app.enums import WithdrawalStatus
from app.models.user import User
from app.models.withdrawal_request import WithdrawalRequest
from app.services.notification import notify_admin

router = Router(name="referral_withdraw")
logger = logging.getLogger(__name__)

_MIN_WITHDRAWAL = Decimal("500")


@router.callback_query(F.data == "referral_withdraw")
async def on_withdraw_request(callback: CallbackQuery) -> None:
    await callback.answer()

    async with async_session_maker() as session:
        user_result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = user_result.scalar_one_or_none()
        if user is None:
            await callback.message.answer("Ошибка: пользователь не найден.")  # type: ignore[union-attr]
            return

        balance = user.referral_balance or Decimal("0")
        if balance < _MIN_WITHDRAWAL:
            await callback.message.answer(  # type: ignore[union-attr]
                f"❌ Ваш баланс <b>{balance:.2f} ₽</b> меньше минимальной суммы вывода "
                f"<b>{_MIN_WITHDRAWAL:.0f} ₽</b>.",
                parse_mode="HTML",
            )
            return

        pending_result = await session.execute(
            select(WithdrawalRequest).where(
                WithdrawalRequest.user_id == user.id,
                WithdrawalRequest.status == WithdrawalStatus.pending,
            )
        )
        if pending_result.scalar_one_or_none() is not None:
            await callback.message.answer(  # type: ignore[union-attr]
                "⏳ У вас уже есть активный запрос на вывод. Ожидайте обработки."
            )
            return

        stars = max(1, math.floor(float(balance) * settings.STARS_PER_RUB))
        req = WithdrawalRequest(
            user_id=user.id,
            amount_rub=balance,
            stars_amount=stars,
            status=WithdrawalStatus.pending,
            created_at=datetime.utcnow(),
        )
        session.add(req)
        user.referral_balance = Decimal("0")
        await session.commit()
        await session.refresh(req)

        req_id = req.id
        amount_rub = balance
        stars_amount = stars
        tg_id = callback.from_user.id
        username = callback.from_user.username

    await callback.message.answer(  # type: ignore[union-attr]
        f"✅ <b>Запрос на вывод #{req_id} отправлен!</b>\n\n"
        f"Сумма: <b>{amount_rub:.2f} ₽</b> → <b>{stars_amount} ⭐ Telegram Stars</b>\n\n"
        f"Администратор рассмотрит запрос в ближайшее время.\n"
        f"После одобрения Stars будут отправлены на ваш Telegram-аккаунт.\n"
        f"Затем вы сможете вывести их через <b>Fragment</b>.",
        parse_mode="HTML",
    )

    mention = f"@{username}" if username else f"tg_id:{tg_id}"
    await notify_admin(
        f"💸 Новый запрос на вывод #{req_id}\n"
        f"Пользователь: {mention}\n"
        f"Сумма: {amount_rub:.2f} ₽ → {stars_amount} ⭐ Stars\n"
        f"Обработать: /admin/withdrawals"
    )
