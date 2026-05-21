from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session_maker
from app.enums import SubscriptionStatus, VpnKeyStatus, VpnServerStatus
from app.models.subscription import Subscription
from app.models.user import User
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.server_migration import (
    _MAX_MIGRATIONS_PER_30_DAYS,
    _MIGRATION_WINDOW_DAYS,
    migrate_key_to_server,
)
from app.services.vpn_key import get_access_url

logger = logging.getLogger(__name__)

router = Router(name="geo_switch")


def _server_list_keyboard(servers: list[VpnServer]) -> InlineKeyboardMarkup:
    rows = []
    for server in servers:
        flag = server.flag_emoji or "🌐"
        city_part = f", {server.city}" if server.city else ""
        label = f"{flag} {server.country}{city_part}"
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"geo_select:{server.id}")]
        )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="back_to_access")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_keyboard(server_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, переключить", callback_data=f"geo_confirm:{server_id}")],
            [InlineKeyboardButton(text="← Отмена", callback_data="back_to_access")],
        ]
    )


async def _get_active_key_for_user(user_id: int) -> tuple[VpnKey | None, int | None]:
    """Return (first active VpnKey, subscription_id) or (None, None)."""
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
            return None, None
        active_key = next((k for k in sub.vpn_keys if k.status == VpnKeyStatus.active), None)
        return active_key, sub.id


@router.callback_query(F.data == "geo_switch")
async def on_geo_switch_start(callback: CallbackQuery, db_user: User) -> None:
    async with async_session_maker() as session:
        result = await session.execute(
            select(VpnServer).where(
                VpnServer.is_selectable.is_(True),
                VpnServer.status == VpnServerStatus.active,
            ).order_by(VpnServer.country)
        )
        servers = result.scalars().all()

    if not servers:
        await callback.answer("Нет доступных серверов для переключения.", show_alert=True)
        return

    await callback.message.answer(  # type: ignore[union-attr]
        "🌍 <b>Выберите сервер</b>\n\nВыберите страну/город для подключения:",
        parse_mode="HTML",
        reply_markup=_server_list_keyboard(servers),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("geo_select:"))
async def on_geo_select(callback: CallbackQuery, db_user: User) -> None:
    try:
        server_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный запрос.", show_alert=True)
        return

    active_key, sub_id = await _get_active_key_for_user(db_user.id)
    if active_key is None:
        await callback.answer("Активный ключ не найден.", show_alert=True)
        return

    if active_key.server_id == server_id:
        await callback.answer("Вы уже подключены к этому серверу.", show_alert=True)
        return

    async with async_session_maker() as session:
        new_server = await session.get(VpnServer, server_id)

    if new_server is None:
        await callback.answer("Сервер не найден.", show_alert=True)
        return

    flag = new_server.flag_emoji or "🌐"
    city_part = f" {new_server.city}" if new_server.city else ""
    server_label = f"{flag} {new_server.country}{city_part}"

    await callback.message.answer(  # type: ignore[union-attr]
        f"Переключить на <b>{server_label}</b>?\n\n"
        f"Текущий ключ будет заменён новым ключом на выбранном сервере.\n"
        f"Лимит смены сервера: {_MAX_MIGRATIONS_PER_30_DAYS} раза за {_MIGRATION_WINDOW_DAYS} дней.",
        parse_mode="HTML",
        reply_markup=_confirm_keyboard(server_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("geo_confirm:"))
async def on_geo_confirm(callback: CallbackQuery, db_user: User) -> None:
    try:
        server_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный запрос.", show_alert=True)
        return

    active_key, sub_id = await _get_active_key_for_user(db_user.id)
    if active_key is None:
        await callback.answer("Активный ключ не найден.", show_alert=True)
        return

    await callback.answer("Переключаем сервер...")

    async with async_session_maker() as session:
        # Re-fetch key and server inside the session
        key_result = await session.execute(
            select(VpnKey).where(VpnKey.id == active_key.id)
        )
        vpn_key = key_result.scalars().first()
        if vpn_key is None:
            await callback.message.answer("Ключ не найден.")  # type: ignore[union-attr]
            return

        new_server = await session.get(VpnServer, server_id)
        if new_server is None:
            await callback.message.answer("Сервер не найден.")  # type: ignore[union-attr]
            return

        try:
            new_key = await migrate_key_to_server(vpn_key, new_server, session)
        except ValueError as exc:
            await callback.message.answer(f"❌ {exc}")  # type: ignore[union-attr]
            return
        except Exception as exc:
            logger.error("Migration failed for user %d: %s", db_user.id, exc)
            await callback.message.answer(  # type: ignore[union-attr]
                "❌ Ошибка при смене сервера. Попробуйте позже или обратитесь в поддержку."
            )
            return

        await session.commit()

    if new_key is None:
        await callback.message.answer(  # type: ignore[union-attr]
            "❌ Не удалось создать ключ на новом сервере. Попробуйте позже."
        )
        return

    try:
        url = get_access_url(new_key)
    except Exception:
        await callback.message.answer(  # type: ignore[union-attr]
            "✅ Сервер переключён, но не удалось получить ключ. Используйте «🔑 Мой ключ»."
        )
        return

    flag = new_server.flag_emoji or "🌐"
    city_part = f" {new_server.city}" if new_server.city else ""
    server_label = f"{flag} {new_server.country}{city_part}"

    await callback.message.answer(  # type: ignore[union-attr]
        f"✅ <b>Сервер переключён на {server_label}!</b>\n\n"
        f"🔑 <b>Ваш новый ключ доступа:</b>\n\n"
        f"<code>{url}</code>\n\n"
        f"Скопируйте ключ и добавьте его в приложение Outline.",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "back_to_access")
async def on_back_to_access(callback: CallbackQuery) -> None:
    await callback.message.delete()  # type: ignore[union-attr]
    await callback.answer()
