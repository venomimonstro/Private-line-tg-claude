"""Customer selection among verified dedicated Marzban VPN nodes."""
from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.database import async_session_maker
from app.enums import SubscriptionStatus, VpnServerStatus
from app.models.vpn_server import VpnServer
from app.models.user import User
from app.services.server_access import ServerSelectionError, add_subscription_server_key
from app.services.vpn_key import get_client_access_url, _server_protocol
from bot.handlers.access import _get_active_subscription

logger = logging.getLogger(__name__)
router = Router(name="geo_switch")


@router.message(Command("servers"))
@router.message(F.text == "🌍 Серверы")
async def cmd_servers(message: Message, db_user: User) -> None:
    sub, active_keys, _ = await _get_active_subscription(db_user.id)
    if sub is None:
        await message.answer("Для выбора VPN-сервера нужна активная подписка.")
        return
    async with async_session_maker() as session:
        servers = [
            srv for srv in (await session.execute(
                select(VpnServer).where(
                    VpnServer.status == VpnServerStatus.active,
                    VpnServer.is_selectable.is_(True),
                    VpnServer.provider != "private-line-primary",
                ).order_by(VpnServer.id.asc())
            )).scalars().all()
            if _server_protocol(srv) == "marzban"
        ]
    if not servers:
        await message.answer("Доступных VPN-нод пока нет.")
        return
    existing = {key.server_id for key in active_keys}
    rows = [[InlineKeyboardButton(
        text=f"{'✅' if srv.id in existing else '➕'} {srv.flag_emoji or '🌍'} {srv.name}",
        callback_data=f"choose_server:{srv.id}"
    )] for srv in servers]
    await message.answer(
        "🌍 <b>Серверы Private Line VPN</b>\n"
        "Выберите ноду. Добавление второй не удалит первый ключ.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "geo_switch")
async def on_geo_switch_start(callback: CallbackQuery, db_user: User) -> None:
    sub, active_keys, _ = await _get_active_subscription(db_user.id)
    if (
        sub is None
        or sub.status not in (SubscriptionStatus.active, SubscriptionStatus.expires_soon)
        or not sub.expires_at
    ):
        await callback.answer("Нет действующей подписки.", show_alert=True)
        return

    async with async_session_maker() as session:
        result = await session.execute(
            select(VpnServer)
            .where(
                VpnServer.status == VpnServerStatus.active,
                VpnServer.is_selectable.is_(True),
                VpnServer.provider != "private-line-primary",
            )
            .order_by(VpnServer.id.asc())
        )
        servers = [
            srv for srv in result.scalars().all()
            if _server_protocol(srv) == "marzban"
        ]

    if not servers:
        await callback.answer("Пока нет доступных VPN-нод.", show_alert=True)
        return

    existing = {key.server_id for key in active_keys}
    rows = [
        [InlineKeyboardButton(
            text=f"{'✅' if srv.id in existing else '➕'} {srv.flag_emoji or '🌍'} {srv.name}",
            callback_data=f"choose_server:{srv.id}",
        )]
        for srv in servers
    ]
    await callback.message.answer(
        "🌍 <b>Выберите VPN-сервер</b>\n\n"
        "✅ Подключённый ключ  •  ➕ Добавить сервер\n"
        "При добавлении второго сервера существующий ключ останется активным.\n"
        "Имя профиля в приложении: <b>Private Line VPN</b>.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("choose_server:"))
async def on_choose_server(callback: CallbackQuery, db_user: User) -> None:
    try:
        server_id = int((callback.data or "").split(":", 1)[1])
        if server_id <= 0:
            raise ValueError()
    except (IndexError, ValueError):
        await callback.answer("Некорректный сервер.", show_alert=True)
        return

    sub, _, _ = await _get_active_subscription(db_user.id)
    if sub is None:
        await callback.answer("Нет активной подписки.", show_alert=True)
        return

    try:
        async with async_session_maker() as session:
            key, created = await add_subscription_server_key(
                session, user_id=db_user.id, subscription_id=sub.id, server_id=server_id
            )
            link = get_client_access_url(key)
            server = await session.get(VpnServer, key.server_id)
            display = escape(server.name if server else "VPN")
            await session.commit()
    except ServerSelectionError as exc:
        await callback.answer(str(exc)[:180], show_alert=True)
        return
    except Exception as exc:
        logger.exception("Node selection failed for user=%s node=%s", db_user.id, server_id)
        await callback.answer(
            "Не удалось подключить ноду. Существующие VPN-ключи сохранены.",
            show_alert=True,
        )
        return

    await callback.message.answer(
        f"{'✅ Сервер добавлен' if created else '✅ Ваш существующий ключ'}: <b>{display}</b>\n\n"
        f"<code>{escape(link)}</code>\n\n"
        "Импортируйте ссылку в Hiddify, v2rayNG, Streisand или V2Box.\n"
        "Название профиля: <b>Private Line VPN</b>.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("geo_view:"))
async def on_geo_view_key(callback: CallbackQuery, db_user: User) -> None:
    try:
        key_id = int((callback.data or "").split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный запрос.", show_alert=True)
        return
    _, keys, _ = await _get_active_subscription(db_user.id)
    key = next((k for k in keys if k.id == key_id), None)
    if key is None:
        await callback.answer("Ключ не найден.", show_alert=True)
        return
    try:
        link = get_client_access_url(key)
    except Exception:
        await callback.answer("Некорректный VPN-ключ.", show_alert=True)
        return
    await callback.message.answer(
        f"🔑 <b>Private Line VPN</b>\n\n<code>{escape(link)}</code>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "back_to_access")
async def on_back_to_access(callback: CallbackQuery) -> None:
    await callback.message.delete()
    await callback.answer()
