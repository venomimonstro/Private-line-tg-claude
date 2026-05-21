from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session_maker
from app.enums import SubscriptionStatus, VpnKeyStatus
from app.models.subscription import Subscription
from app.models.user import User
from app.models.vpn_key import VpnKey
from app.services.vpn_key import get_access_url

router = Router(name="mykey")


async def _get_active_keys(user_id: int) -> list[VpnKey]:
    """Return all active VPN keys for the given user."""
    async with async_session_maker() as session:
        result = await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.vpn_keys))
            .where(
                Subscription.user_id == user_id,
                Subscription.status.in_([
                    SubscriptionStatus.active,
                    SubscriptionStatus.expires_soon,
                ]),
            )
            .order_by(Subscription.expires_at.desc())
        )
        sub = result.scalars().first()
        if sub is None:
            return []
        return [k for k in sub.vpn_keys if k.status == VpnKeyStatus.active]


def _keys_keyboard(keys: list[VpnKey]) -> InlineKeyboardMarkup:
    rows = []
    for idx, key in enumerate(keys, start=1):
        label = key.label or f"Ключ {idx}"
        rows.append(
            [InlineKeyboardButton(text=f"🔑 {label}", callback_data=f"get_key:{key.id}")]
        )
        rows.append(
            [InlineKeyboardButton(text=f"📨 Переслать {label}", callback_data=f"resend_key:{key.id}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("mykey"))
@router.message(F.text == "🔑 Мой ключ")
async def cmd_mykey(message: Message, db_user: User) -> None:
    keys = await _get_active_keys(db_user.id)

    if not keys:
        await message.answer(
            "У вас нет активных ключей.\n\nНажмите «💳 Купить доступ», чтобы оформить подписку."
        )
        return

    if len(keys) == 1:
        key = keys[0]
        try:
            url = get_access_url(key)
        except Exception:
            await message.answer("Ошибка получения ключа. Обратитесь в поддержку.")
            return

        label_part = f" ({key.label})" if key.label else ""
        await message.answer(
            f"🔑 <b>Ваш персональный ключ доступа{label_part}:</b>\n\n"
            f"<code>{url}</code>\n\n"
            f"Скопируйте ключ и добавьте его в приложение Outline.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"У вас активно <b>{len(keys)} ключа</b>. Выберите нужный:",
            parse_mode="HTML",
            reply_markup=_keys_keyboard(keys),
        )


@router.callback_query(F.data.startswith("resend_key:"))
async def on_resend_key(callback: CallbackQuery, db_user: User) -> None:
    try:
        key_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный запрос.", show_alert=True)
        return

    keys = await _get_active_keys(db_user.id)
    key = next((k for k in keys if k.id == key_id), None)

    if key is None:
        await callback.answer("Ключ не найден или не принадлежит вам.", show_alert=True)
        return

    try:
        url = get_access_url(key)
    except Exception:
        await callback.answer("Ошибка получения ключа. Обратитесь в поддержку.", show_alert=True)
        return

    label_part = f" ({key.label})" if key.label else ""
    await callback.message.answer(  # type: ignore[union-attr]
        f"📨 <b>Ключ доступа{label_part} повторно:</b>\n\n"
        f"<code>{url}</code>\n\n"
        f"Скопируйте ключ и добавьте его в приложение Outline.",
        parse_mode="HTML",
    )
    await callback.answer("Ключ отправлен повторно.")
