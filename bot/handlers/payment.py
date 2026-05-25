from __future__ import annotations

from aiogram import F, Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.database import async_session_maker
from app.enums import SubscriptionStatus
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.models.user import User
from app.payments.services import create_payment
from bot.keyboards.main_menu import tariff_selection_keyboard

router = Router(name="payment")


@router.message(F.text == "🔄 Продлить")
async def handle_renew(message: Message, db_user: User) -> None:
    async with async_session_maker() as session:
        sub_result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == db_user.id,
                Subscription.status.in_([
                    SubscriptionStatus.active,
                    SubscriptionStatus.expires_soon,
                    SubscriptionStatus.expired,
                ]),
            ).order_by(Subscription.expires_at.desc())
        )
        current_sub = sub_result.scalars().first()

        if current_sub is None:
            tariff_result = await session.execute(
                select(Tariff)
                .where(Tariff.is_active == True, Tariff.is_visible == True)  # noqa: E712
                .order_by(Tariff.sort_order)
            )
            tariffs = tariff_result.scalars().all()
            await message.answer(
                "Выберите тариф для оформления доступа:",
                reply_markup=tariff_selection_keyboard(tariffs),
            )
            return

        tariff = await session.get(Tariff, current_sub.tariff_id)
        if tariff is None:
            await message.answer("Тариф не найден. Обратитесь в поддержку.")
            return

        payment, payment_result = await create_payment(db_user.id, tariff.slug, session)
        await session.commit()

    from app.payments.registry import get_provider
    provider = get_provider()

    expires_str = current_sub.expires_at.strftime("%d.%m.%Y") if current_sub.expires_at else "—"

    if payment_result.payment_url:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=payment_result.payment_url)],
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"recheck_payment:{payment.id}")],
        ])
        await message.answer(
            f"🔄 <b>Продление подписки</b>\n\n"
            f"Тариф: {tariff.name}\n"
            f"Действует до: {expires_str}\n"
            f"Сумма к оплате: <b>{int(payment.amount)} RUB</b>\n\n"
            f"Нажмите кнопку ниже для оплаты:",
            parse_mode="HTML",
            reply_markup=markup,
        )
    elif payment_result.display_data.get("stars_flow"):
        from bot.handlers.stars_payment import send_stars_invoice
        await send_stars_invoice(
            message.bot,
            db_user.telegram_id,
            payment.id,
            float(payment.amount),
            f"Private Line — {tariff.name} (продление)",
        )
    else:
        instructions = (
            provider.get_instructions()  # type: ignore[attr-defined]
            if hasattr(provider, "get_instructions")
            else payment_result.display_data.get("instructions", "")
        )
        await message.answer(
            f"🔄 <b>Продление подписки</b>\n\n"
            f"Тариф: {tariff.name}\n"
            f"Действует до: {expires_str}\n"
            f"Сумма к оплате: <b>{int(payment.amount)} RUB</b>\n\n"
            f"📋 <b>Инструкция по оплате:</b>\n{instructions}\n\n"
            f"В комментарии укажите: <code>#{payment.id}</code>\n\n"
            f"После подтверждения оплаты срок подписки будет продлён.",
            parse_mode="HTML",
        )
