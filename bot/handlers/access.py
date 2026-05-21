from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session_maker
from app.enums import SubscriptionStatus, VpnKeyStatus
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.models.user import User
from app.models.vpn_key import VpnKey
from app.services.vpn_key import get_access_url
from bot.keyboards.main_menu import connection_platforms_keyboard, my_access_keyboard

router = Router(name="access")

_STATUS_LABELS: dict[str, str] = {
    SubscriptionStatus.active: "✅ Активна",
    SubscriptionStatus.expires_soon: "⚠️ Истекает скоро",
    SubscriptionStatus.expired: "❌ Истекла",
    SubscriptionStatus.pending_payment: "⏳ Ожидает оплаты",
    SubscriptionStatus.disabled: "🔒 Отключена",
    SubscriptionStatus.blocked: "🚫 Заблокирована",
    SubscriptionStatus.activation_error: "⚠️ Ошибка активации",
}

_CONNECTION_INSTRUCTIONS = {
    "android": (
        "📱 <b>Android — инструкция подключения</b>\n\n"
        "1. Установите приложение <b>Outline</b> из Google Play\n"
        "2. Скопируйте ваш персональный ключ (кнопка «Получить ключ»)\n"
        "3. В приложении Outline нажмите «+» и вставьте ключ\n"
        "4. Нажмите «Подключиться»\n\n"
        "Готово! Соединение защищено."
    ),
    "iphone": (
        "🍎 <b>iPhone — инструкция подключения</b>\n\n"
        "1. Установите приложение <b>Outline</b> из App Store\n"
        "2. Скопируйте ваш персональный ключ (кнопка «Получить ключ»)\n"
        "3. В приложении Outline нажмите «+» и вставьте ключ\n"
        "4. Нажмите «Подключиться»\n\n"
        "Готово! Соединение защищено."
    ),
    "windows": (
        "🖥 <b>Windows — инструкция подключения</b>\n\n"
        "1. Скачайте приложение <b>Outline</b> с официального сайта getoutline.org\n"
        "2. Скопируйте ваш персональный ключ (кнопка «Получить ключ»)\n"
        "3. В приложении Outline нажмите «+» и вставьте ключ\n"
        "4. Нажмите «Подключиться»\n\n"
        "Готово! Соединение защищено."
    ),
    "macos": (
        "🍏 <b>macOS — инструкция подключения</b>\n\n"
        "1. Скачайте приложение <b>Outline</b> из Mac App Store\n"
        "2. Скопируйте ваш персональный ключ (кнопка «Получить ключ»)\n"
        "3. В приложении Outline нажмите «+» и вставьте ключ\n"
        "4. Нажмите «Подключиться»\n\n"
        "Готово! Соединение защищено."
    ),
}


async def _get_active_subscription(
    user_id: int,
) -> tuple[Subscription | None, VpnKey | None, Tariff | None]:
    async with async_session_maker() as session:
        sub_result = await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.vpn_keys))
            .where(
                Subscription.user_id == user_id,
                Subscription.status.in_([
                    SubscriptionStatus.active,
                    SubscriptionStatus.expires_soon,
                    SubscriptionStatus.activation_error,
                ]),
            )
            .order_by(Subscription.expires_at.desc())
        )
        sub = sub_result.scalars().first()
        if sub is None:
            return None, None, None

        active_key = next(
            (k for k in sub.vpn_keys if k.status == VpnKeyStatus.active),
            None,
        )

        tariff = await session.get(Tariff, sub.tariff_id)
        return sub, active_key, tariff


@router.message(F.text == "🔑 Мой доступ")
@router.message(Command("my_access"))
async def show_my_access(message: Message, db_user: User) -> None:
    sub, active_key, tariff = await _get_active_subscription(db_user.id)

    if sub is None:
        await message.answer(
            "У вас нет активного доступа.\n\nНажмите «💳 Купить доступ», чтобы оформить подписку.",
            reply_markup=my_access_keyboard(),
        )
        return

    status_label = _STATUS_LABELS.get(sub.status, sub.status)
    expires_str = sub.expires_at.strftime("%d.%m.%Y") if sub.expires_at else "—"
    traffic_used = f"{active_key.traffic_used_gb:.1f}" if active_key else "—"
    tariff_name = tariff.name if tariff else "—"
    traffic_limit_str = f"{tariff.traffic_limit_gb} ГБ/мес" if (tariff and tariff.traffic_limit_gb) else "Без ограничений"

    text = (
        f"<b>Ваш доступ</b>\n\n"
        f"📋 Тариф: <b>{tariff_name}</b>\n"
        f"📊 Статус: {status_label}\n"
        f"📅 Действует до: <b>{expires_str}</b>\n"
        f"🌍 Сервер: Германия\n"
        f"📦 Лимит трафика: <b>{traffic_limit_str}</b>\n"
        f"📈 Использовано: {traffic_used} ГБ"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=my_access_keyboard())


@router.callback_query(F.data == "get_key")
async def on_get_key(callback: CallbackQuery, db_user: User) -> None:
    sub, active_key, tariff = await _get_active_subscription(db_user.id)

    if sub is None or active_key is None:
        await callback.answer("Активный ключ не найден. Обратитесь в поддержку.", show_alert=True)
        return

    try:
        access_url = get_access_url(active_key)
    except Exception:
        await callback.answer("Ошибка получения ключа. Обратитесь в поддержку.", show_alert=True)
        return

    await callback.message.answer(  # type: ignore[union-attr]
        f"🔑 <b>Ваш персональный ключ доступа:</b>\n\n"
        f"<code>{access_url}</code>\n\n"
        f"Скопируйте ключ и добавьте его в приложение Outline.\n"
        f"Нажмите «📱 Как подключиться» для инструкции.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(F.text == "📱 Как подключиться")
async def show_connection_guide(message: Message) -> None:
    await message.answer(
        "Выберите вашу платформу для получения инструкции подключения:",
        reply_markup=connection_platforms_keyboard(),
    )


@router.callback_query(F.data.startswith("instructions:"))
async def on_platform_instructions(callback: CallbackQuery) -> None:
    platform = callback.data.split(":", 1)[1]
    text = _CONNECTION_INSTRUCTIONS.get(platform)
    if text is None:
        await callback.answer("Инструкция не найдена.", show_alert=True)
        return
    await callback.message.answer(text, parse_mode="HTML")  # type: ignore[union-attr]
    await callback.answer()


@router.message(F.text == "📊 Проверить лимит")
async def check_limit(message: Message, db_user: User) -> None:
    sub, active_key, tariff = await _get_active_subscription(db_user.id)

    if sub is None or active_key is None:
        await message.answer("У вас нет активной подписки.")
        return

    used = active_key.traffic_used_gb
    limit = tariff.traffic_limit_gb if tariff else None

    if limit and limit > 0:
        percent = (used / limit * 100)
        text = (
            f"📊 <b>Использование трафика</b>\n\n"
            f"Использовано: <b>{used:.2f} ГБ</b> из {limit} ГБ\n"
            f"Осталось: <b>{max(0.0, limit - used):.2f} ГБ</b>\n"
            f"{'▓' * int(percent // 10)}{'░' * (10 - int(percent // 10))} {percent:.1f}%"
        )
    else:
        text = (
            f"📊 <b>Использование трафика</b>\n\n"
            f"Использовано: <b>{used:.2f} ГБ</b>\n"
            f"Лимит: Без ограничений"
        )
    await message.answer(text, parse_mode="HTML")
