from __future__ import annotations

import math
from decimal import Decimal

from aiogram import F, Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database import async_session_maker
from app.models.user import User
from app.models.withdrawal_request import WithdrawalRequest
from app.enums import WithdrawalStatus
from sqlalchemy import select

router = Router(name="referral")

_MIN_WITHDRAWAL = Decimal("500")
_REVSHARE_PERCENT = 10


@router.message(F.text == "👥 Пригласить друга")
async def invite_friend(message: Message, db_user: User) -> None:
    bot_info = await message.bot.get_me()  # type: ignore[union-attr]
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{db_user.telegram_id}"

    async with async_session_maker() as session:
        # Refresh user to get current balance
        user = await session.get(User, db_user.id)
        balance = user.referral_balance if user and user.referral_balance else Decimal("0")

        # Check if there's already a pending withdrawal request
        pending_result = await session.execute(
            select(WithdrawalRequest).where(
                WithdrawalRequest.user_id == db_user.id,
                WithdrawalRequest.status == WithdrawalStatus.pending,
            )
        )
        has_pending = pending_result.scalar_one_or_none() is not None

    balance_text = (
        f"\n\n💰 <b>Ваш реферальный баланс:</b> <b>{balance:.2f} ₽</b>"
    )

    if balance >= _MIN_WITHDRAWAL and not has_pending:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💸 Запросить вывод", callback_data="referral_withdraw")],
        ])
        withdraw_hint = f"\n✅ Вы можете запросить вывод — минимальная сумма {_MIN_WITHDRAWAL:.0f} ₽ достигнута!"
    elif has_pending:
        kb = None
        withdraw_hint = "\n⏳ Запрос на вывод уже отправлен и ожидает обработки."
    else:
        kb = None
        remaining = _MIN_WITHDRAWAL - balance
        withdraw_hint = f"\n🔒 До вывода осталось накопить ещё <b>{remaining:.2f} ₽</b>."

    text = (
        f"👥 <b>Реферальная программа</b>\n\n"
        f"Приглашайте друзей и получайте <b>{_REVSHARE_PERCENT}% от каждого их платежа</b> пожизненно!\n\n"
        f"Когда друг оформит первую подписку, вам также добавятся бонусные дни.\n\n"
        f"Ваша персональная ссылка:\n"
        f"<code>{ref_link}</code>"
        f"{balance_text}"
        f"{withdraw_hint}"
    )

    await message.answer(text, parse_mode="HTML", reply_markup=kb)
