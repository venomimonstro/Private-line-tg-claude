from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal

from aiogram import F, Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database import async_session_maker
from app.models.referral import Referral
from app.models.referral_transaction import ReferralTransaction
from app.models.user import User
from app.models.withdrawal_request import WithdrawalRequest
from app.enums import WithdrawalStatus
from sqlalchemy import select

router = Router(name="referral")

_MIN_WITHDRAWAL = Decimal("500")
_REVSHARE_PERCENT = 10


def _build_stats_text(
    referrals: list,
    transactions: list,
    now: datetime,
) -> str:
    cutoff_7 = now - timedelta(days=7)
    cutoff_30 = now - timedelta(days=30)
    cutoff_60 = now - timedelta(days=60)

    # Per-day breakdown for last 7 days
    days_7 = [(now - timedelta(days=i)).date() for i in range(6, -1, -1)]
    reg_by_day: dict = {d: 0 for d in days_7}
    pay_by_day: dict = {d: 0 for d in days_7}
    income_by_day: dict = {d: Decimal("0") for d in days_7}

    for ref in referrals:
        d = ref.created_at.date()
        if d in reg_by_day:
            reg_by_day[d] += 1

    for tx in transactions:
        d = tx.created_at.date()
        if d in pay_by_day:
            pay_by_day[d] += 1
            income_by_day[d] += tx.amount_rub

    # 7-day table
    rows = ["<code>Дата   Перех  Оплат   Доход₽</code>"]
    has_any_7 = False
    for d in days_7:
        regs = reg_by_day[d]
        pays = pay_by_day[d]
        inc = income_by_day[d]
        if regs or pays or inc:
            has_any_7 = True
        rows.append(
            f"<code>{d.strftime('%d.%m')}    {regs:>3}    {pays:>3}   {float(inc):>7.2f}</code>"
        )

    table = "\n".join(rows)

    # 30/60 day totals
    refs_30 = sum(1 for r in referrals if r.created_at >= cutoff_30)
    refs_60 = sum(1 for r in referrals if r.created_at >= cutoff_60)
    pays_30 = sum(1 for t in transactions if t.created_at >= cutoff_30)
    pays_60 = sum(1 for t in transactions if t.created_at >= cutoff_60)
    income_30 = sum(t.amount_rub for t in transactions if t.created_at >= cutoff_30)
    income_60 = sum(t.amount_rub for t in transactions if t.created_at >= cutoff_60)

    stats = (
        f"\n\n📊 <b>Статистика рефералов</b>\n\n"
        f"<b>По дням (7 дней):</b>\n"
        f"{table}\n\n"
        f"<b>30 дней:</b> {refs_30} переходов, {pays_30} оплат, {float(income_30):.2f} ₽\n"
        f"<b>60 дней:</b> {refs_60} переходов, {pays_60} оплат, {float(income_60):.2f} ₽\n"
        f"<b>Доход за месяц:</b> {float(income_30):.2f} ₽"
    )
    return stats


@router.message(F.text == "👥 Пригласить друга")
async def invite_friend(message: Message, db_user: User) -> None:
    bot_info = await message.bot.get_me()  # type: ignore[union-attr]
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{db_user.telegram_id}"

    now = datetime.utcnow()
    cutoff_60 = now - timedelta(days=60)

    async with async_session_maker() as session:
        user = await session.get(User, db_user.id)
        balance = user.referral_balance if user and user.referral_balance else Decimal("0")

        pending_result = await session.execute(
            select(WithdrawalRequest).where(
                WithdrawalRequest.user_id == db_user.id,
                WithdrawalRequest.status == WithdrawalStatus.pending,
            )
        )
        has_pending = pending_result.scalar_one_or_none() is not None

        refs_result = await session.execute(
            select(Referral)
            .where(Referral.referrer_id == db_user.id, Referral.created_at >= cutoff_60)
            .order_by(Referral.created_at)
        )
        referrals = refs_result.scalars().all()

        txs_result = await session.execute(
            select(ReferralTransaction)
            .where(ReferralTransaction.referrer_id == db_user.id, ReferralTransaction.created_at >= cutoff_60)
            .order_by(ReferralTransaction.created_at)
        )
        transactions = txs_result.scalars().all()

    balance_text = f"\n\n💰 <b>Ваш реферальный баланс:</b> <b>{balance:.2f} ₽</b>"

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

    stats_text = _build_stats_text(referrals, transactions, now)

    text = (
        f"👥 <b>Реферальная программа</b>\n\n"
        f"Приглашайте друзей и получайте <b>{_REVSHARE_PERCENT}% от каждого их платежа</b> пожизненно!\n\n"
        f"Когда друг оформит первую подписку, вам также добавятся бонусные дни.\n\n"
        f"Ваша персональная ссылка:\n"
        f"<code>{ref_link}</code>"
        f"{balance_text}"
        f"{withdraw_hint}"
        f"{stats_text}"
    )

    await message.answer(text, parse_mode="HTML", reply_markup=kb)
