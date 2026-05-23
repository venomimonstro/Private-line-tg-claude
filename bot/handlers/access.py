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
from bot.handlers.onboarding import CONNECTION_INSTRUCTIONS
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


async def _get_active_subscription(
    user_id: int,
) -> tuple[Subscription | None, list[VpnKey], Tariff | None]:
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
            return None, [], None

        active_keys = [k for k in sub.vpn_keys if k.status == VpnKeyStatus.active]

        tariff = await session.get(Tariff, sub.tariff_id)
        return sub, active_keys, tariff


@router.message(F.text == "🔑 Мой доступ")
@router.message(Command("my_access"))
async def show_my_access(message: Message, db_user: User) -> None:
    sub, active_keys, tariff = await _get_active_subscription(db_user.id)

    if sub is None:
        await message.answer(
            "У вас нет активного доступа.\n\nНажмите «💳 Купить доступ», чтобы оформить подписку.",
            reply_markup=my_access_keyboard(),
        )
        return

    active_key = active_keys[0] if active_keys else None
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
        f"📦 Лимит трафика: <b>{traffic_limit_str}</b>\n"
        f"📈 Использовано: {traffic_used} ГБ"
    )

    await message.answer(text, parse_mode="HTML", reply_markup=my_access_keyboard())


@router.callback_query(F.data == "get_key")
async def on_get_key(callback: CallbackQuery, db_user: User) -> None:
    sub, active_keys, tariff = await _get_active_subscription(db_user.id)

    if sub is None or not active_keys:
        await callback.answer("Активный ключ не найден. Обратитесь в поддержку.", show_alert=True)
        return

    if len(active_keys) == 1:
        key = active_keys[0]
        try:
            access_url = get_access_url(key)
        except Exception:
            await callback.answer("Ошибка получения ключа. Обратитесь в поддержку.", show_alert=True)
            return
        label_part = f" ({key.label})" if key.label else ""
        await callback.message.answer(  # type: ignore[union-attr]
            f"🔑 <b>Ваш ключ доступа{label_part}:</b>\n\n"
            f"<code>{access_url}</code>\n\n"
            f"Скопируйте ключ и импортируйте в приложение:\n"
            f"• <b>V2RayNG</b> (Android)\n• <b>Streisand / FoXray</b> (iPhone)\n"
            f"• <b>Hiddify</b> (Windows/Mac)",
            parse_mode="HTML",
        )
    else:
        lines = ["🔑 <b>Ваши ключи доступа (выберите любой):</b>\n"]
        for key in active_keys:
            try:
                url = get_access_url(key)
            except Exception:
                continue
            label = key.label or "VPN"
            lines.append(f"<b>{label}:</b>\n<code>{url}</code>")
        lines.append(
            "\n📱 Скопируйте любой ключ и добавьте в приложение:\n"
            "• <b>V2RayNG</b> (Android)\n• <b>Streisand / FoXray</b> (iPhone)\n"
            "• <b>Hiddify</b> (Windows/Mac)"
        )
        await callback.message.answer("\n\n".join(lines), parse_mode="HTML")  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data.startswith("get_key:"))
async def on_get_key_by_id(callback: CallbackQuery, db_user: User) -> None:
    try:
        key_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный запрос.", show_alert=True)
        return

    sub, active_keys, tariff = await _get_active_subscription(db_user.id)

    if sub is None:
        await callback.answer("Активный ключ не найден. Обратитесь в поддержку.", show_alert=True)
        return

    target_key = next((k for k in active_keys if k.id == key_id), None)
    if target_key is None:
        await callback.answer("Ключ не найден или не принадлежит вам.", show_alert=True)
        return

    try:
        access_url = get_access_url(target_key)
    except Exception:
        await callback.answer("Ошибка получения ключа. Обратитесь в поддержку.", show_alert=True)
        return

    label_part = f" ({target_key.label})" if target_key.label else ""
    await callback.message.answer(  # type: ignore[union-attr]
        f"🔑 <b>Ваш персональный ключ доступа{label_part}:</b>\n\n"
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
    text = CONNECTION_INSTRUCTIONS.get(platform)
    if text is None:
        await callback.answer("Инструкция не найдена.", show_alert=True)
        return
    await callback.message.answer(text, parse_mode="HTML")  # type: ignore[union-attr]
    await callback.answer()


@router.message(F.text == "📊 Проверить лимит")
async def check_limit(message: Message, db_user: User) -> None:
    sub, active_keys, tariff = await _get_active_subscription(db_user.id)

    if sub is None or not active_keys:
        await message.answer("У вас нет активной подписки.")
        return

    active_key = active_keys[0]
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
