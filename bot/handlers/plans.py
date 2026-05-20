from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_maker
from app.models.tariff import Tariff
from app.models.user import User
from app.payments.manual import ManualPaymentProvider
from app.payments.services import create_payment
from bot.keyboards.main_menu import accept_rules_keyboard, tariff_selection_keyboard

router = Router(name="plans")

_RULES_TEXT = (
    "<b>Правила сервиса Private Line</b>\n\n"
    "1. Сервис предназначен для защищённого интернет-доступа.\n"
    "2. Запрещено использование для незаконной деятельности, спама, фишинга, DDoS-атак, торрентов.\n"
    "3. Запрещена передача ключа доступа третьим лицам.\n"
    "4. Сервис вправе заблокировать доступ при нарушении правил без возврата средств.\n"
    "5. Подписка не продлевается автоматически в MVP-режиме.\n"
    "6. При технических неполадках поддержка решит вопрос вручную.\n\n"
    "Нажимая «Я принимаю правила», вы соглашаетесь с условиями использования."
)


class PurchaseStates(StatesGroup):
    selecting_tariff = State()
    accepting_rules = State()


@router.message(F.text == "💳 Купить доступ")
@router.message(Command("plans"))
async def show_plans(message: Message, state: FSMContext) -> None:
    async with async_session_maker() as session:
        result = await session.execute(
            select(Tariff)
            .where(Tariff.is_active == True, Tariff.is_visible == True)  # noqa: E712
            .order_by(Tariff.sort_order)
        )
        tariffs = result.scalars().all()

    if not tariffs:
        await message.answer("В данный момент нет доступных тарифов. Попробуйте позже.")
        return

    await state.set_state(PurchaseStates.selecting_tariff)
    await message.answer(
        "Выберите тариф для защищённого доступа:",
        reply_markup=tariff_selection_keyboard(tariffs),
    )


@router.callback_query(F.data.startswith("tariff:"))
async def on_tariff_selected(callback: CallbackQuery, state: FSMContext) -> None:
    tariff_code = callback.data.split(":", 1)[1]

    async with async_session_maker() as session:
        result = await session.execute(
            select(Tariff).where(Tariff.code == tariff_code, Tariff.is_active == True)  # noqa: E712
        )
        tariff = result.scalar_one_or_none()

    if tariff is None:
        await callback.answer("Тариф недоступен.", show_alert=True)
        return

    await state.update_data(tariff_code=tariff_code)
    await state.set_state(PurchaseStates.accepting_rules)

    await callback.message.edit_text(  # type: ignore[union-attr]
        f"<b>Тариф «{tariff.name}»</b>\n\n"
        f"💰 Стоимость: {int(tariff.price)} ₽/мес\n"
        f"📦 Трафик: {tariff.traffic_limit_gb} ГБ/мес\n"
        f"📱 Устройств: {tariff.devices_limit}\n\n"
        + _RULES_TEXT,
        parse_mode="HTML",
        reply_markup=accept_rules_keyboard(tariff_code),
    )
    await callback.answer()


@router.callback_query(F.data == "back_to_plans")
async def back_to_plans(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    async with async_session_maker() as session:
        result = await session.execute(
            select(Tariff)
            .where(Tariff.is_active == True, Tariff.is_visible == True)  # noqa: E712
            .order_by(Tariff.sort_order)
        )
        tariffs = result.scalars().all()
    await callback.message.edit_text(  # type: ignore[union-attr]
        "Выберите тариф для защищённого доступа:",
        reply_markup=tariff_selection_keyboard(tariffs),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("accept_rules:"))
async def on_accept_rules(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    from datetime import datetime
    tariff_code = callback.data.split(":", 1)[1]

    async with async_session_maker() as session:
        result = await session.execute(
            select(Tariff).where(Tariff.code == tariff_code)
        )
        tariff = result.scalar_one_or_none()
        if tariff is None:
            await callback.answer("Тариф не найден.", show_alert=True)
            return

        from sqlalchemy import select as sa_select
        from app.models.user import User as UserModel
        user_result = await session.execute(sa_select(UserModel).where(UserModel.id == db_user.id))
        user = user_result.scalar_one()
        user.rules_accepted_at = datetime.utcnow()
        await session.flush()

        payment = await create_payment(db_user.id, tariff_code, session)
        await session.commit()

    provider = ManualPaymentProvider()
    instructions = provider.get_payment_instructions()

    await callback.message.edit_text(  # type: ignore[union-attr]
        f"✅ Правила приняты.\n\n"
        f"<b>Заявка #{payment.id} создана.</b>\n"
        f"Тариф: {tariff.name}\n"
        f"Сумма к оплате: <b>{int(payment.amount)} {payment.currency}</b>\n\n"
        f"📋 <b>Инструкция по оплате:</b>\n{instructions}\n\n"
        f"В комментарии к переводу укажите: <code>#{payment.id}</code>\n\n"
        f"После поступления оплаты вы получите персональный ключ доступа.",
        parse_mode="HTML",
    )
    await state.clear()
    await callback.answer()
