from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.database import async_session_maker
from app.models.tariff import Tariff
from app.models.user import User
from app.payments.services import create_payment, get_enabled_providers
from bot.keyboards.main_menu import accept_rules_keyboard, tariff_selection_keyboard

_PROVIDER_LABELS: dict[str, str] = {
    "yookassa":  "💳 ЮKassa (карта / СБП)",
    "yoomoney":  "💛 ЮMoney",
    "freekassa": "💳 FreeKassa",
    "lava":      "🌋 LAVA",
    "robokassa": "🤖 Robokassa",
    "prodamus":  "💳 Prodamus",
    "crypto":    "🪙 Крипта (TON / USDT / BTC)",
    "stars":     "⭐ Telegram Stars",
    "manual":    "🏦 Ручная оплата",
}

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
    waiting_promo_code = State()
    confirming_purchase = State()


def _provider_selection_keyboard(
    providers: list[str], tariff_slug: str, promo_code: str | None
) -> InlineKeyboardMarkup:
    promo_part = promo_code or ""
    buttons = [
        [InlineKeyboardButton(
            text=_PROVIDER_LABELS.get(p, p),
            callback_data=f"pay_via:{p}:{tariff_slug}:{promo_part}",
        )]
        for p in providers
    ]
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="back_to_plans")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _promo_prompt_keyboard(tariff_slug: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"skip_promo:{tariff_slug}")],
            [InlineKeyboardButton(text="← Назад", callback_data="back_to_plans")],
        ]
    )


def _confirm_purchase_keyboard(tariff_slug: str, has_promo: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="✅ Я принимаю правила сервиса", callback_data=f"accept_rules:{tariff_slug}")],
    ]
    if not has_promo:
        buttons.append([InlineKeyboardButton(text="🎟 Ввести промокод", callback_data=f"enter_promo:{tariff_slug}")])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="back_to_plans")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


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
        "Выберите тариф для защищённого доступа:\n\n"
        "💎 Есть промокод? Введите его после выбора тарифа.",
        reply_markup=tariff_selection_keyboard(tariffs),
    )


@router.callback_query(F.data.startswith("tariff:"))
async def on_tariff_selected(callback: CallbackQuery, state: FSMContext) -> None:
    tariff_slug = callback.data.split(":", 1)[1]  # type: ignore[union-attr]

    async with async_session_maker() as session:
        result = await session.execute(
            select(Tariff).where(Tariff.slug == tariff_slug, Tariff.is_active == True)  # noqa: E712
        )
        tariff = result.scalar_one_or_none()

    if tariff is None:
        await callback.answer("Тариф недоступен.", show_alert=True)
        return

    await state.update_data(tariff_slug=tariff_slug, promo_code=None, promo_discount=None)
    await state.set_state(PurchaseStates.accepting_rules)

    traffic_str = f"{tariff.traffic_limit_gb} ГБ/мес" if tariff.traffic_limit_gb else "Без ограничений"

    await callback.message.edit_text(  # type: ignore[union-attr]
        f"<b>Тариф «{tariff.name}»</b>\n\n"
        f"💰 Стоимость: {int(tariff.price_monthly)} ₽/мес\n"
        f"📦 Трафик: {traffic_str}\n"
        f"📱 Устройств: {tariff.max_keys}\n\n"
        + _RULES_TEXT,
        parse_mode="HTML",
        reply_markup=_confirm_purchase_keyboard(tariff_slug),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("enter_promo:"))
async def on_enter_promo(callback: CallbackQuery, state: FSMContext) -> None:
    tariff_slug = callback.data.split(":", 1)[1]  # type: ignore[union-attr]
    await state.update_data(tariff_slug=tariff_slug)
    await state.set_state(PurchaseStates.waiting_promo_code)

    await callback.message.edit_text(  # type: ignore[union-attr]
        "🎟 <b>Введите промокод</b>\n\n"
        "У вас есть промокод? Введите его в чат или нажмите «Пропустить».",
        parse_mode="HTML",
        reply_markup=_promo_prompt_keyboard(tariff_slug),
    )
    await callback.answer()


@router.message(PurchaseStates.waiting_promo_code)
async def on_promo_code_entered(message: Message, state: FSMContext, db_user: User) -> None:
    code = (message.text or "").strip()
    data = await state.get_data()
    tariff_slug = data.get("tariff_slug", "")

    async with async_session_maker() as session:
        from app.services.promo import validate_promo

        promo, error = await validate_promo(code, tariff_slug, db_user.id, session)

        if promo is None:
            await message.answer(
                f"❌ {error}\n\nВведите другой промокод или нажмите «Пропустить».",
                reply_markup=_promo_prompt_keyboard(tariff_slug),
            )
            return

        # Valid promo — fetch tariff details for display
        tariff_result = await session.execute(
            select(Tariff).where(Tariff.slug == tariff_slug)
        )
        tariff = tariff_result.scalar_one_or_none()

    if tariff is None:
        await message.answer("Тариф не найден. Пожалуйста, начните заново.")
        await state.clear()
        return

    if promo.is_trial:
        trial_days = promo.trial_days or 7
        trial_traffic = promo.trial_traffic_gb or 1
        await state.update_data(promo_code=code, is_trial=True)
        await state.set_state(PurchaseStates.confirming_purchase)

        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="🆓 Попробовать бесплатно",
                    callback_data=f"confirm_trial:{tariff_slug}:{code}",
                )],
                [InlineKeyboardButton(text="← Назад", callback_data="back_to_plans")],
            ]
        )
        await message.answer(
            f"✅ Промокод <b>{code}</b> активирован!\n\n"
            f"🆓 Пробный период: <b>{trial_days} дней</b>, {trial_traffic} ГБ трафика\n\n"
            "Нажмите кнопку ниже для активации:",
            parse_mode="HTML",
            reply_markup=markup,
        )
    else:
        # Compute discounted price for display
        base_price = float(tariff.price_monthly)
        discounted_price = base_price
        if promo.discount_percent:
            discounted_price = base_price * (1 - promo.discount_percent / 100.0)
        elif promo.discount_fixed:
            discounted_price = base_price - float(promo.discount_fixed)
        discounted_price = max(1.0, discounted_price)

        await state.update_data(promo_code=code, is_trial=False, discounted_price=discounted_price)
        await state.set_state(PurchaseStates.accepting_rules)

        traffic_str = f"{tariff.traffic_limit_gb} ГБ/мес" if tariff.traffic_limit_gb else "Без ограничений"

        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="✅ Я принимаю правила сервиса",
                    callback_data=f"accept_rules_promo:{tariff_slug}:{code}",
                )],
                [InlineKeyboardButton(text="← Назад", callback_data="back_to_plans")],
            ]
        )
        await message.answer(
            f"<b>Тариф «{tariff.name}»</b>\n\n"
            f"💰 Стоимость: <s>{int(base_price)} ₽</s> → <b>{int(discounted_price)} ₽/мес</b>\n"
            f"📦 Трафик: {traffic_str}\n"
            f"📱 Устройств: {tariff.max_keys}\n\n"
            f"🎟 Промокод <b>{code}</b> применён!\n\n"
            + _RULES_TEXT,
            parse_mode="HTML",
            reply_markup=markup,
        )


@router.callback_query(F.data.startswith("skip_promo:"))
async def on_skip_promo(callback: CallbackQuery, state: FSMContext) -> None:
    tariff_slug = callback.data.split(":", 1)[1]  # type: ignore[union-attr]
    await state.update_data(promo_code=None, is_trial=False)
    await state.set_state(PurchaseStates.accepting_rules)

    async with async_session_maker() as session:
        result = await session.execute(
            select(Tariff).where(Tariff.slug == tariff_slug, Tariff.is_active == True)  # noqa: E712
        )
        tariff = result.scalar_one_or_none()

    if tariff is None:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    traffic_str = f"{tariff.traffic_limit_gb} ГБ/мес" if tariff.traffic_limit_gb else "Без ограничений"

    await callback.message.edit_text(  # type: ignore[union-attr]
        f"<b>Тариф «{tariff.name}»</b>\n\n"
        f"💰 Стоимость: {int(tariff.price_monthly)} ₽/мес\n"
        f"📦 Трафик: {traffic_str}\n"
        f"📱 Устройств: {tariff.max_keys}\n\n"
        + _RULES_TEXT,
        parse_mode="HTML",
        reply_markup=accept_rules_keyboard(tariff_slug),
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
        "Выберите тариф для защищённого доступа:\n\n"
        "💎 Есть промокод? Введите его после выбора тарифа.",
        reply_markup=tariff_selection_keyboard(tariffs),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_trial:"))
async def on_confirm_trial(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    """Handle trial subscription activation from promo code."""
    parts = callback.data.split(":", 2)  # type: ignore[union-attr]
    if len(parts) < 3:
        await callback.answer("Ошибка данных.", show_alert=True)
        return

    tariff_slug = parts[1]
    promo_code = parts[2]

    async with async_session_maker() as session:
        from app.services.promo import create_trial_subscription, validate_promo

        promo, error = await validate_promo(promo_code, tariff_slug, db_user.id, session)
        if promo is None:
            await callback.answer(f"❌ {error}", show_alert=True)
            return

        subscription = await create_trial_subscription(db_user.id, tariff_slug, promo, session)
        if subscription is None:
            await callback.answer(
                "❌ Пробный период уже был использован или недоступен.",
                show_alert=True,
            )
            return

        await session.commit()

    await callback.message.edit_text(  # type: ignore[union-attr]
        "🎉 <b>Пробный период активирован!</b>\n\n"
        "Ваша подписка будет активирована в течение нескольких минут.\n"
        "Вы получите уведомление с ключом доступа.",
        parse_mode="HTML",
    )
    await state.clear()
    await callback.answer()


@router.callback_query(F.data.startswith("accept_rules:"))
async def on_accept_rules(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    from datetime import datetime
    tariff_slug = callback.data.split(":", 1)[1]  # type: ignore[union-attr]

    await _process_payment(callback, state, db_user, tariff_slug, promo_code=None)


@router.callback_query(F.data.startswith("accept_rules_promo:"))
async def on_accept_rules_promo(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    """Handle rules acceptance with a promo code."""
    parts = callback.data.split(":", 2)  # type: ignore[union-attr]
    if len(parts) < 3:
        await callback.answer("Ошибка данных.", show_alert=True)
        return

    tariff_slug = parts[1]
    promo_code = parts[2]

    await _process_payment(callback, state, db_user, tariff_slug, promo_code=promo_code)


async def _process_payment(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: User,
    tariff_slug: str,
    promo_code: str | None,
) -> None:
    """Mark rules accepted, then show provider selection or go straight to payment."""
    from datetime import datetime

    async with async_session_maker() as session:
        merged_user = await session.get(User, db_user.id)
        if merged_user is not None:
            merged_user.rules_accepted_at = datetime.utcnow()
            await session.flush()
        await session.commit()

        providers = await get_enabled_providers(session)

    if len(providers) == 1:
        await _do_create_payment(callback, state, db_user, tariff_slug, promo_code, providers[0])
    else:
        await callback.message.edit_text(  # type: ignore[union-attr]
            "✅ Правила приняты.\n\n<b>Выберите способ оплаты:</b>",
            parse_mode="HTML",
            reply_markup=_provider_selection_keyboard(providers, tariff_slug, promo_code),
        )
        await callback.answer()


@router.callback_query(F.data.startswith("pay_via:"))
async def on_pay_via_selected(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    parts = callback.data.split(":", 3)  # type: ignore[union-attr]
    if len(parts) < 3:
        await callback.answer("Ошибка данных.", show_alert=True)
        return
    provider_name = parts[1]
    tariff_slug = parts[2]
    promo_code = parts[3] if len(parts) > 3 and parts[3] else None
    await _do_create_payment(callback, state, db_user, tariff_slug, promo_code, provider_name)


async def _do_create_payment(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: User,
    tariff_slug: str,
    promo_code: str | None,
    provider_name: str,
) -> None:
    """Create payment with a specific provider and send result to user."""
    async with async_session_maker() as session:
        result = await session.execute(select(Tariff).where(Tariff.slug == tariff_slug))
        tariff = result.scalar_one_or_none()
        if tariff is None:
            await callback.answer("Тариф не найден.", show_alert=True)
            return

        from app.services.promo import apply_promo, validate_promo
        from decimal import Decimal

        promo_obj = None
        if promo_code:
            promo_obj, _error = await validate_promo(promo_code, tariff_slug, db_user.id, session)

        payment, payment_result = await create_payment(
            db_user.id, tariff_slug, session, provider_name=provider_name
        )

        if promo_obj is not None:
            discounted_amount = await apply_promo(promo_obj, float(payment.amount), session)
            payment.amount = Decimal(str(discounted_amount))
            await session.flush()

        await session.commit()

    promo_line = "\n🎟 Промокод применён! Скидка учтена." if promo_obj else ""

    if payment_result.payment_url:
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💳 Оплатить", url=payment_result.payment_url)
        ]])
        await callback.message.edit_text(  # type: ignore[union-attr]
            f"<b>Заявка #{payment.id}</b>\n"
            f"Тариф: {tariff.name}\n"
            f"Сумма: <b>{int(payment.amount)} RUB</b>{promo_line}\n\n"
            "Нажмите кнопку ниже для оплаты:",
            parse_mode="HTML",
            reply_markup=markup,
        )
    elif payment_result.display_data.get("stars_flow"):
        from bot.handlers.stars_payment import send_stars_invoice
        await callback.message.delete()  # type: ignore[union-attr]
        await send_stars_invoice(
            callback.bot,
            db_user.telegram_id,
            payment.id,
            float(payment.amount),
            f"Private Line — {tariff.name}",
        )
    else:
        from app.payments.registry import get_provider
        provider = get_provider(provider_name)
        instructions = (
            provider.get_instructions()  # type: ignore[attr-defined]
            if hasattr(provider, "get_instructions")
            else payment_result.display_data.get("instructions", "Ожидайте инструкций от поддержки.")
        )
        await callback.message.edit_text(  # type: ignore[union-attr]
            f"<b>Заявка #{payment.id} создана.</b>\n"
            f"Тариф: {tariff.name}\n"
            f"Сумма к оплате: <b>{int(payment.amount)} RUB</b>{promo_line}\n\n"
            f"📋 <b>Инструкция по оплате:</b>\n{instructions}\n\n"
            f"В комментарии укажите: <code>#{payment.id}</code>\n\n"
            "После поступления оплаты вы получите ключ доступа.",
            parse_mode="HTML",
        )

    await state.clear()
    await callback.answer()
