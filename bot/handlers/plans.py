"""Payment flow: tariff selection → rules → method selection → pay."""
from __future__ import annotations

import logging
import math
from datetime import datetime
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.config import settings
from app.database import async_session_maker
from app.models.tariff import Tariff
from app.models.user import User
from bot.keyboards.main_menu import tariff_selection_keyboard

logger = logging.getLogger(__name__)

router = Router(name="plans")

# ── display labels ────────────────────────────────────────────────────────────
_LABELS: dict[str, str] = {
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

_RULES = (
    "<b>Правила сервиса Private Line</b>\n\n"
    "1. Сервис предназначен для защищённого интернет-доступа.\n"
    "2. Запрещено: незаконная деятельность, спам, фишинг, DDoS, торренты.\n"
    "3. Запрещена передача ключа доступа третьим лицам.\n"
    "4. Нарушение правил → блокировка без возврата средств.\n"
    "5. Подписка не продлевается автоматически.\n\n"
    "Нажав «Принять и выбрать оплату», вы соглашаетесь с правилами."
)


class PurchaseStates(StatesGroup):
    selecting_tariff = State()
    accepting_rules = State()
    waiting_promo_code = State()


# ── keyboards ─────────────────────────────────────────────────────────────────

def _kb_tariff_rules(tariff_slug: str) -> InlineKeyboardMarkup:
    """Rules screen buttons."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Принять и выбрать оплату",
            callback_data=f"rules_ok:{tariff_slug}:",   # last segment = promo (empty)
        )],
        [InlineKeyboardButton(text="🎟 Ввести промокод", callback_data=f"enter_promo:{tariff_slug}")],
        [InlineKeyboardButton(text="← Назад к тарифам", callback_data="back_to_plans")],
    ])


def _kb_tariff_rules_promo(tariff_slug: str, promo: str) -> InlineKeyboardMarkup:
    """Rules screen buttons with promo already entered."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Принять и выбрать оплату",
            callback_data=f"rules_ok:{tariff_slug}:{promo}",
        )],
        [InlineKeyboardButton(text="← Назад к тарифам", callback_data="back_to_plans")],
    ])


def _kb_providers(providers: list[str], tariff_slug: str, promo: str) -> InlineKeyboardMarkup:
    """Payment method selection buttons."""
    rows = [
        [InlineKeyboardButton(
            text=_LABELS.get(p, p),
            callback_data=f"do_pay:{p}:{tariff_slug}:{promo}",
        )]
        for p in providers
    ]
    rows.append([InlineKeyboardButton(text="← Назад к тарифам", callback_data="back_to_plans")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_promo_skip(tariff_slug: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"skip_promo:{tariff_slug}")],
        [InlineKeyboardButton(text="← Назад к тарифам", callback_data="back_to_plans")],
    ])


def _kb_stars_pay(payment_id: int, stars: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"⭐ Оплатить {stars} Stars",
            callback_data=f"stars_pay:{payment_id}",
        )
    ]])


def _kb_pay_url(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💳 Перейти к оплате", url=url)
    ]])


# ── helpers ───────────────────────────────────────────────────────────────────

async def _load_tariff(tariff_slug: str) -> Tariff | None:
    async with async_session_maker() as session:
        r = await session.execute(
            select(Tariff).where(Tariff.slug == tariff_slug, Tariff.is_active == True)  # noqa: E712
        )
        return r.scalar_one_or_none()


async def _load_providers() -> list[str]:
    from app.payments.services import get_enabled_providers
    async with async_session_maker() as session:
        return await get_enabled_providers(session)


def _tariff_summary(tariff: Tariff) -> str:
    traffic = f"{tariff.traffic_limit_gb} ГБ/мес" if tariff.traffic_limit_gb else "Безлимит"
    if tariff.price_first_month is not None:
        price_line = (
            f"💰 <s>{int(tariff.price_monthly)} ₽</s> → <b>{int(tariff.price_first_month)} ₽</b> в первый месяц\n"
            f"   затем {int(tariff.price_monthly)} ₽/мес"
        )
    else:
        price_line = f"💰 <b>{int(tariff.price_monthly)} ₽/мес</b>"
    desc_line = f"\n📝 {tariff.description}" if tariff.description else ""
    return (
        f"<b>Тариф «{tariff.name}»</b>{desc_line}\n\n"
        f"{price_line}\n"
        f"📦 Трафик: {traffic}\n"
        f"📱 Устройств: {tariff.max_keys}"
    )


# ── step 1: show tariff list ──────────────────────────────────────────────────

@router.message(F.text == "💳 Купить доступ")
@router.message(Command("plans"))
async def show_plans(message: Message, state: FSMContext) -> None:
    async with async_session_maker() as session:
        r = await session.execute(
            select(Tariff)
            .where(Tariff.is_active == True, Tariff.is_visible == True)  # noqa: E712
            .order_by(Tariff.sort_order)
        )
        tariffs = list(r.scalars().all())

    if not tariffs:
        await message.answer("В данный момент нет доступных тарифов. Попробуйте позже.")
        return

    await state.set_state(PurchaseStates.selecting_tariff)
    await message.answer(
        "Выберите тариф:",
        reply_markup=tariff_selection_keyboard(tariffs),
    )


# ── step 2: show rules for chosen tariff ─────────────────────────────────────

@router.callback_query(F.data.startswith("tariff:"))
async def on_tariff_selected(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    tariff_slug = callback.data.split(":", 1)[1]  # type: ignore[union-attr]

    tariff = await _load_tariff(tariff_slug)
    if tariff is None:
        await callback.message.answer("❌ Тариф недоступен.")  # type: ignore[union-attr]
        return

    await state.update_data(tariff_slug=tariff_slug)
    await state.set_state(PurchaseStates.accepting_rules)

    await callback.message.edit_text(  # type: ignore[union-attr]
        _tariff_summary(tariff) + "\n\n" + _RULES,
        parse_mode="HTML",
        reply_markup=_kb_tariff_rules(tariff_slug),
    )


# ── promo code flow ───────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("enter_promo:"))
async def on_enter_promo(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    tariff_slug = callback.data.split(":", 1)[1]  # type: ignore[union-attr]
    await state.update_data(tariff_slug=tariff_slug)
    await state.set_state(PurchaseStates.waiting_promo_code)
    await callback.message.edit_text(  # type: ignore[union-attr]
        "🎟 Введите промокод или нажмите «Пропустить»:",
        reply_markup=_kb_promo_skip(tariff_slug),
    )


@router.callback_query(F.data.startswith("skip_promo:"))
async def on_skip_promo(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    tariff_slug = callback.data.split(":", 1)[1]  # type: ignore[union-attr]
    tariff = await _load_tariff(tariff_slug)
    if tariff is None:
        await callback.message.answer("❌ Тариф недоступен.")  # type: ignore[union-attr]
        return
    await state.set_state(PurchaseStates.accepting_rules)
    await callback.message.edit_text(  # type: ignore[union-attr]
        _tariff_summary(tariff) + "\n\n" + _RULES,
        parse_mode="HTML",
        reply_markup=_kb_tariff_rules(tariff_slug),
    )


@router.message(PurchaseStates.waiting_promo_code)
async def on_promo_entered(message: Message, state: FSMContext, db_user: User) -> None:
    code = (message.text or "").strip()
    data = await state.get_data()
    tariff_slug = data.get("tariff_slug", "")

    async with async_session_maker() as session:
        from app.services.promo import validate_promo
        promo, error = await validate_promo(code, tariff_slug, db_user.id, session)

    if promo is None:
        await message.answer(
            f"❌ {error}\n\nВведите другой промокод или нажмите «Пропустить».",
            reply_markup=_kb_promo_skip(tariff_slug),
        )
        return

    if promo.is_trial:
        # Trial promo — confirm and activate immediately
        await state.update_data(promo_code=code)
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🆓 Активировать пробный период",
                callback_data=f"confirm_trial:{tariff_slug}:{code}",
            )],
            [InlineKeyboardButton(text="← Назад", callback_data="back_to_plans")],
        ])
        await message.answer(
            f"✅ Промокод <b>{code}</b> — пробный период {promo.trial_days or 7} дн.\n\n"
            "Нажмите кнопку ниже:",
            parse_mode="HTML",
            reply_markup=markup,
        )
        return

    # Discount promo — go back to rules screen with promo applied
    tariff = await _load_tariff(tariff_slug)
    if tariff is None:
        await message.answer("❌ Тариф недоступен.")
        await state.clear()
        return

    base = float(tariff.price_monthly)
    disc = base
    if promo.discount_percent:
        disc = base * (1 - promo.discount_percent / 100.0)
    elif promo.discount_fixed:
        disc = base - float(promo.discount_fixed)
    disc = max(1.0, disc)

    await state.update_data(promo_code=code)
    await state.set_state(PurchaseStates.accepting_rules)

    await message.answer(
        _tariff_summary(tariff)
        + f"\n💸 Цена со скидкой: <b>{int(disc)} ₽/мес</b> (скидка от промокода <b>{code}</b>)\n\n"
        + _RULES,
        parse_mode="HTML",
        reply_markup=_kb_tariff_rules_promo(tariff_slug, code),
    )


# ── step 3: rules accepted → show payment methods ─────────────────────────────

@router.callback_query(F.data.startswith("rules_ok:"))
async def on_rules_accepted(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    await callback.answer()

    # parse: rules_ok:{tariff_slug}:{promo}
    parts = (callback.data or "").split(":", 2)  # type: ignore[union-attr]
    tariff_slug = parts[1] if len(parts) > 1 else ""
    promo_code = parts[2] if len(parts) > 2 else ""

    # save rules acceptance
    try:
        async with async_session_maker() as session:
            u = await session.get(User, db_user.id)
            if u:
                u.rules_accepted_at = datetime.utcnow()
            await session.commit()
    except Exception:
        logger.exception("rules_ok: failed to save rules_accepted_at")

    # load available providers
    try:
        providers = await _load_providers()
    except Exception:
        logger.exception("rules_ok: failed to load providers")
        await callback.message.answer(  # type: ignore[union-attr]
            "❌ Ошибка загрузки методов оплаты. Попробуйте позже."
        )
        await state.clear()
        return

    if not providers:
        await callback.message.answer(  # type: ignore[union-attr]
            "❌ Нет доступных методов оплаты. Обратитесь в поддержку."
        )
        await state.clear()
        return

    promo_note = f"\n🎟 Промокод: <b>{promo_code}</b>" if promo_code else ""
    await callback.message.answer(  # type: ignore[union-attr]
        f"✅ Правила приняты.{promo_note}\n\n"
        "<b>Выберите способ оплаты:</b>",
        parse_mode="HTML",
        reply_markup=_kb_providers(providers, tariff_slug, promo_code),
    )
    await state.clear()


# ── step 4: provider chosen → create payment ─────────────────────────────────

@router.callback_query(F.data.startswith("do_pay:"))
async def on_do_pay(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    await callback.answer()

    # parse: do_pay:{provider}:{tariff_slug}:{promo}
    parts = (callback.data or "").split(":", 3)  # type: ignore[union-attr]
    if len(parts) < 3:
        await callback.message.answer("❌ Ошибка данных. Начните заново /plans")  # type: ignore[union-attr]
        return

    provider_name = parts[1]
    tariff_slug = parts[2]
    promo_code = parts[3] if len(parts) > 3 else ""

    try:
        from app.payments.services import create_payment
        from app.services.promo import apply_promo, validate_promo

        async with async_session_maker() as session:
            # verify tariff exists
            r = await session.execute(select(Tariff).where(Tariff.slug == tariff_slug))
            tariff = r.scalar_one_or_none()
            if tariff is None:
                await callback.message.answer("❌ Тариф не найден. Начните заново /plans")  # type: ignore[union-attr]
                return

            # validate promo (if any)
            promo_obj = None
            if promo_code:
                promo_obj, _err = await validate_promo(promo_code, tariff_slug, db_user.id, session)

            # create payment record + provider intent
            payment, result = await create_payment(
                db_user.id, tariff_slug, session, provider_name=provider_name
            )

            # apply discount
            if promo_obj is not None:
                disc = await apply_promo(promo_obj, float(payment.amount), session)
                payment.amount = Decimal(str(disc))
                await session.flush()

            # capture values before session closes
            payment_id = payment.id
            amount = float(payment.amount)
            tariff_name = tariff.name
            payment_url = result.payment_url
            stars_flow = result.display_data.get("stars_flow", False)
            instructions = result.display_data.get("instructions", "")

            await session.commit()

    except Exception:
        logger.exception("on_do_pay error (provider=%s tariff=%s)", provider_name, tariff_slug)
        await callback.message.answer(  # type: ignore[union-attr]
            "❌ Ошибка при создании платежа. Попробуйте позже или обратитесь в поддержку."
        )
        return

    promo_line = f"\n🎟 Промокод применён." if promo_code and promo_obj else ""

    if payment_url:
        await callback.message.answer(  # type: ignore[union-attr]
            f"<b>Счёт #{payment_id}</b>\n"
            f"Тариф: {tariff_name}\n"
            f"Сумма: <b>{int(amount)} ₽</b>{promo_line}\n\n"
            "Нажмите кнопку для оплаты:",
            parse_mode="HTML",
            reply_markup=_kb_pay_url(payment_url),
        )

    elif stars_flow:
        stars = max(1, math.ceil(amount * settings.STARS_PER_RUB))
        await callback.message.answer(  # type: ignore[union-attr]
            f"<b>Счёт #{payment_id}</b>\n"
            f"Тариф: {tariff_name}\n"
            f"Сумма: <b>{stars} ⭐</b> (~{int(amount)} ₽){promo_line}\n\n"
            "Нажмите кнопку — откроется окно оплаты Telegram Stars:",
            parse_mode="HTML",
            reply_markup=_kb_stars_pay(payment_id, stars),
        )

    else:
        # manual / unknown provider
        if not instructions:
            instructions = "Ожидайте инструкций от поддержки."
        await callback.message.answer(  # type: ignore[union-attr]
            f"<b>Заявка #{payment_id} создана.</b>\n"
            f"Тариф: {tariff_name}\n"
            f"Сумма: <b>{int(amount)} ₽</b>{promo_line}\n\n"
            f"📋 {instructions}\n\n"
            f"В комментарии укажите: <code>#{payment_id}</code>",
            parse_mode="HTML",
        )


# ── step 5 (stars only): send invoice ────────────────────────────────────────

@router.callback_query(F.data.startswith("stars_pay:"))
async def on_stars_pay(callback: CallbackQuery, db_user: User) -> None:
    await callback.answer()

    try:
        payment_id = int((callback.data or "").split(":", 1)[1])  # type: ignore[union-attr]
    except (ValueError, IndexError):
        await callback.message.answer("❌ Ошибка данных платежа.")  # type: ignore[union-attr]
        return

    try:
        from app.enums import PaymentStatus
        from app.models.payment import Payment as PaymentModel

        async with async_session_maker() as session:
            payment = await session.get(PaymentModel, payment_id)
            if payment is None:
                await callback.message.answer("❌ Платёж не найден.")  # type: ignore[union-attr]
                return
            if payment.user_id != db_user.id:
                await callback.message.answer("❌ Нет доступа.")  # type: ignore[union-attr]
                return
            if payment.status != PaymentStatus.pending:
                await callback.message.answer("ℹ️ Этот платёж уже обработан.")  # type: ignore[union-attr]
                return

            amount = float(payment.amount)
            tid = payment.tariff_id

        async with async_session_maker() as session:
            t = await session.get(Tariff, tid)
            tname = t.name if t else "Private Line"

    except Exception:
        logger.exception("stars_pay: DB error payment_id=%d", payment_id)
        await callback.message.answer("❌ Ошибка сервера. Попробуйте позже.")  # type: ignore[union-attr]
        return

    try:
        from bot.handlers.stars_payment import send_stars_invoice
        await send_stars_invoice(
            callback.bot,
            db_user.telegram_id,
            payment_id,
            amount,
            f"Private Line — {tname}",
        )
    except Exception:
        logger.exception("stars_pay: send_invoice failed payment_id=%d", payment_id)
        await callback.message.answer(  # type: ignore[union-attr]
            "❌ Не удалось выставить счёт Stars. "
            "Убедитесь, что бот настроен для приёма Stars, или обратитесь в поддержку."
        )


# ── trial promo confirm ───────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("confirm_trial:"))
async def on_confirm_trial(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    await callback.answer()
    parts = (callback.data or "").split(":", 2)  # type: ignore[union-attr]
    if len(parts) < 3:
        await callback.message.answer("❌ Ошибка данных.")  # type: ignore[union-attr]
        return

    tariff_slug, promo_code = parts[1], parts[2]

    try:
        async with async_session_maker() as session:
            from app.services.promo import create_trial_subscription, validate_promo

            promo, error = await validate_promo(promo_code, tariff_slug, db_user.id, session)
            if promo is None:
                await callback.message.answer(f"❌ {error}")  # type: ignore[union-attr]
                return

            sub = await create_trial_subscription(db_user.id, tariff_slug, promo, session)
            if sub is None:
                await callback.message.answer(  # type: ignore[union-attr]
                    "❌ Пробный период уже использован или недоступен."
                )
                return

            await session.commit()
    except Exception:
        logger.exception("confirm_trial error")
        await callback.message.answer("❌ Ошибка сервера. Попробуйте позже.")  # type: ignore[union-attr]
        return

    await callback.message.answer(  # type: ignore[union-attr]
        "🎉 <b>Пробный период активирован!</b>\n\n"
        "Ваша подписка будет активирована в течение нескольких минут.\n"
        "Вы получите ключ доступа.",
        parse_mode="HTML",
    )
    await state.clear()


# ── back to plans ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "back_to_plans")
async def back_to_plans(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()

    async with async_session_maker() as session:
        r = await session.execute(
            select(Tariff)
            .where(Tariff.is_active == True, Tariff.is_visible == True)  # noqa: E712
            .order_by(Tariff.sort_order)
        )
        tariffs = list(r.scalars().all())

    await callback.message.answer(  # type: ignore[union-attr]
        "Выберите тариф:",
        reply_markup=tariff_selection_keyboard(tariffs),
    )
