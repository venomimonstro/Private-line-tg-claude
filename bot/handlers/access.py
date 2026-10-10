from __future__ import annotations

import logging
from html import escape
from datetime import date, datetime as _dt

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session_maker
from app.encryption import decrypt
from app.enums import SubscriptionStatus, VpnKeyStatus, VpnServerStatus
from app.models.subscription import Subscription
from app.models.tariff import Tariff
from app.models.user import User
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.vpn_key import get_client_access_url, _server_protocol
from bot.handlers.onboarding import CONNECTION_INSTRUCTIONS
from bot.keyboards.main_menu import connection_platforms_keyboard, my_access_keyboard, reissue_confirm_keyboard

logger = logging.getLogger(__name__)

router = Router(name="access")

_MAX_DAILY_REISSUES = 5

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
                ]),
                Subscription.expires_at > _dt.utcnow(),
            )
            .order_by(Subscription.expires_at.desc())
        )
        sub = sub_result.scalars().first()
        if sub is None:
            return None, [], None

        keys_result = await session.execute(
            select(VpnKey)
            .join(VpnServer, VpnServer.id == VpnKey.server_id)
            .where(
                VpnKey.subscription_id == sub.id,
                VpnKey.user_id == user_id,
                VpnKey.status == VpnKeyStatus.active,
                VpnServer.status != VpnServerStatus.disabled,
                VpnServer.provider != "private-line-primary",
            )
            .order_by(VpnServer.id.asc(), VpnKey.id.asc())
        )
        candidate_keys = list(keys_result.scalars().all())
        active_keys = []
        for key in candidate_keys:
            server = await session.get(VpnServer, key.server_id)
            if server is not None and _server_protocol(server) == "marzban":
                active_keys.append(key)

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

    keys_count = len(active_keys)
    max_keys = max(tariff.max_keys if tariff else 1, keys_count)
    max_dev = tariff.max_devices if tariff else 0
    dev_str = f"до {max_dev}" if max_dev else "без ограничений"

    text = (
        f"<b>Ваш доступ</b>\n\n"
        f"📋 Тариф: <b>{tariff_name}</b>\n"
        f"📊 Статус: {status_label}\n"
        f"📅 Действует до: <b>{expires_str}</b>\n"
        f"📦 Лимит трафика: <b>{traffic_limit_str}</b>\n"
        f"📈 Использовано: {traffic_used} ГБ\n"
        f"🔑 VPN-ключей: <b>{keys_count} из {max_keys}</b>\n"
        f"📱 Устройств одновременно: <b>{dev_str}</b>"
    )

    await message.answer(text, parse_mode="HTML", reply_markup=my_access_keyboard())


@router.callback_query(F.data == "get_key")
async def on_get_key(callback: CallbackQuery, db_user: User) -> None:
    sub, active_keys, tariff = await _get_active_subscription(db_user.id)

    if sub is None or not active_keys:
        await callback.message.answer(  # type: ignore[union-attr]
            "⏳ <b>Ключ ещё создаётся или не был выдан</b>\n\n"
            "Если вы только что оплатили — подождите 1-2 минуты, ключ выдаётся автоматически.\n\n"
            "Если прошло более 5 минут — напишите в поддержку.",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    if len(active_keys) == 1:
        key = active_keys[0]
        try:
            access_url = get_client_access_url(key)
        except Exception:
            await callback.answer("Ошибка получения ключа. Обратитесь в поддержку.", show_alert=True)
            return
        label_part = f" ({key.label})" if key.label else ""
        await callback.message.answer(  # type: ignore[union-attr]
            f"🔑 <b>Ваш ключ доступа{label_part}:</b>\n\n"
            f"<code>{escape(access_url)}</code>\n\n"
            f"📱 Приложения:\n"
            f"• <b>Android:</b> Hiddify, V2RayNG\n"
            f"• <b>iPhone:</b> Streisand, FoXray, V2Box\n"
            f"• <b>Windows/macOS:</b> Hiddify\n"
            f"• <b>Linux:</b> Hiddify, v2rayA\n\n"
            f"Нажмите «📱 Как подключиться» для пошаговых инструкций.",
            parse_mode="HTML",
        )
    else:
        lines = ["🔑 <b>Ваши ключи доступа (выберите любой):</b>\n"]
        for key in active_keys:
            try:
                url = get_client_access_url(key)
            except Exception:
                continue
            label = key.label or "VPN"
            lines.append(f"<b>{label}:</b>\n<code>{escape(url)}</code>")
        lines.append(
            "\n📱 Скопируйте любой ключ и добавьте в приложение:\n"
            "• <b>Android:</b> Hiddify, V2RayNG\n"
            "• <b>iPhone:</b> Streisand, FoXray, V2Box\n"
            "• <b>Windows/macOS:</b> Hiddify\n"
            "• <b>Linux:</b> Hiddify, v2rayA\n\n"
            "Нажмите «📱 Как подключиться» для пошаговых инструкций."
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
        access_url = get_client_access_url(target_key)
    except Exception:
        await callback.answer("Ошибка получения ключа. Обратитесь в поддержку.", show_alert=True)
        return

    label_part = f" ({target_key.label})" if target_key.label else ""
    await callback.message.answer(  # type: ignore[union-attr]
        f"🔑 <b>Ваш персональный ключ доступа{label_part}:</b>\n\n"
        f"<code>{escape(access_url)}</code>\n\n"
        f"Скопируйте ключ и добавьте в приложение:\n"
        f"• <b>Android:</b> Hiddify, V2RayNG\n"
        f"• <b>iPhone:</b> Streisand, FoXray, V2Box\n"
        f"• <b>Windows/macOS:</b> Hiddify\n\n"
        f"Нажмите «📱 Как подключиться» для пошаговой инструкции.",
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


async def _fetch_live_traffic_gb(key: VpnKey) -> float | None:
    """Query VPN server directly for this key's live traffic. Returns GB or None on error.

    Intentionally bypasses the circuit breaker so a user-initiated
    request always reaches the server (CB is a batch-job guard, not a
    user-facing gate).  A 10-second timeout protects the bot handler.
    """
    import asyncio
    from datetime import datetime as _dt

    from app.models.vpn_server import VpnServer
    from app.services.vpn_key import _make_client

    try:
        async with async_session_maker() as session:
            server = await session.get(VpnServer, key.server_id)
            if server is None:
                logger.warning("Traffic check: server %d not found for key %d", key.server_id, key.id)
                return None

            client = _make_client(server)
            try:
                stats: dict = await asyncio.wait_for(
                    client.get_transfer_stats(),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Traffic check timed out for key %d (server %d)", key.id, key.server_id)
                return None
            finally:
                await client.aclose()

            # Outline uses numeric string IDs; Marzban uses username strings.
            # outline_key_id is already a str column — try it directly.
            bytes_used: int | None = stats.get(key.outline_key_id)
            if bytes_used is None:
                # Fallback: int key in dict (Outline sometimes stores as int)
                try:
                    bytes_used = stats.get(int(key.outline_key_id))  # type: ignore[arg-type]
                except (ValueError, TypeError):
                    pass

            if bytes_used is None:
                logger.debug(
                    "Traffic check: key %r not found in stats (server %d). "
                    "Known keys: %s",
                    key.outline_key_id, key.server_id,
                    list(stats.keys())[:10],
                )
                return 0.0

            used_gb = bytes_used / (1024 ** 3)

            # Persist the fresh value so the background sync stays consistent
            key_row = await session.get(VpnKey, key.id)
            if key_row is not None:
                key_row.traffic_used_gb = used_gb
                key_row.last_checked_at = _dt.utcnow()
                await session.commit()

            return used_gb

    except Exception as exc:
        logger.warning("Traffic check failed for key %d: %s", key.id, exc, exc_info=True)
        return None


@router.message(F.text == "📊 Проверить лимит")
async def check_limit(message: Message, db_user: User) -> None:
    sub, active_keys, tariff = await _get_active_subscription(db_user.id)

    if sub is None or not active_keys:
        await message.answer("У вас нет активной подписки.")
        return

    active_key = active_keys[0]

    # Try live query first; fall back to cached value if server is unreachable
    live_gb = await _fetch_live_traffic_gb(active_key)
    used = live_gb if live_gb is not None else active_key.traffic_used_gb
    source_note = "" if live_gb is not None else "\n<i>⚠️ Данные из кэша — сервер временно недоступен</i>"

    limit = tariff.traffic_limit_gb if tariff else None

    if limit and limit > 0:
        percent = min(100.0, used / limit * 100)
        filled = int(percent // 10)
        text = (
            f"📊 <b>Использование трафика</b>\n\n"
            f"Использовано: <b>{used:.2f} ГБ</b> из {limit} ГБ\n"
            f"Осталось: <b>{max(0.0, limit - used):.2f} ГБ</b>\n"
            f"{'▓' * filled}{'░' * (10 - filled)} {percent:.1f}%"
            f"{source_note}"
        )
    else:
        text = (
            f"📊 <b>Использование трафика</b>\n\n"
            f"Использовано: <b>{used:.2f} ГБ</b>\n"
            f"Лимит: <b>Без ограничений</b>"
            f"{source_note}"
        )
    await message.answer(text, parse_mode="HTML")


# ─── Key self-service reissue ────────────────────────────────────────────────

@router.callback_query(F.data == "reissue_key")
async def on_reissue_key(callback: CallbackQuery, db_user: User) -> None:
    """Show confirmation prompt with daily limit status."""
    sub, active_keys, tariff = await _get_active_subscription(db_user.id)

    if sub is None or not active_keys:
        await callback.answer(
            "Нет активного ключа для обновления.", show_alert=True
        )
        return

    key = active_keys[0]
    today = date.today()
    used = key.reissue_count_today if key.reissue_date == today else 0
    remaining = _MAX_DAILY_REISSUES - used

    if remaining <= 0:
        await callback.answer(
            f"⛔ Лимит {_MAX_DAILY_REISSUES} обновлений в день исчерпан.\n"
            "Попробуйте завтра.",
            show_alert=True,
        )
        return

    await callback.message.answer(  # type: ignore[union-attr]
        f"🔄 <b>Обновить ключ доступа?</b>\n\n"
        f"Текущий ключ будет аннулирован и заменён новым.\n"
        f"После обновления старый ключ перестанет работать — "
        f"импортируйте новый в приложение.\n\n"
        f"📊 Использовано обновлений сегодня: <b>{used}/{_MAX_DAILY_REISSUES}</b>",
        parse_mode="HTML",
        reply_markup=reissue_confirm_keyboard(used, _MAX_DAILY_REISSUES),
    )
    await callback.answer()


@router.callback_query(F.data == "reissue_confirm")
async def on_reissue_confirm(callback: CallbackQuery, db_user: User) -> None:
    """Safely refresh the current key profile without destructive rotation."""
    await callback.answer("⏳ Проверяем и обновляем профиль…")

    async with async_session_maker() as session:
        # Re-read everything inside the session for consistency
        sub_result = await session.execute(
            select(Subscription)
            .where(
                Subscription.user_id == db_user.id,
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
            await callback.message.answer(  # type: ignore[union-attr]
                "❌ Активная подписка не найдена.", parse_mode="HTML"
            )
            return

        keys_result = await session.execute(
            select(VpnKey).where(
                VpnKey.subscription_id == sub.id,
                VpnKey.status == VpnKeyStatus.active,
            )
        )
        active_key = keys_result.scalars().first()
        if active_key is None:
            await callback.message.answer(  # type: ignore[union-attr]
                "❌ Активный ключ не найден.", parse_mode="HTML"
            )
            return

        # Double-check daily limit (guard against concurrent taps)
        today = date.today()
        used = active_key.reissue_count_today if active_key.reissue_date == today else 0
        if used >= _MAX_DAILY_REISSUES:
            await callback.message.answer(  # type: ignore[union-attr]
                f"⛔ Лимит {_MAX_DAILY_REISSUES} обновлений в день исчерпан.\n"
                "Попробуйте завтра.",
                parse_mode="HTML",
            )
            return

        server = await session.get(VpnServer, active_key.server_id)
        if server is None:
            await callback.message.answer(  # type: ignore[union-attr]
                "❌ VPN-сервер не найден. Обратитесь в поддержку.", parse_mode="HTML"
            )
            return

        try:
            # User-facing "refresh" is preserve-first: reconcile Marzban and
            # regenerate the client profile without rotating UUID/Reality keys.
            from app.services.vpn_reconcile import reconcile_marzban_server
            result = await reconcile_marzban_server(
                session,
                server.id,
                apply=True,
                only_subscription_id=sub.id,
            )
            if result.errors:
                raise RuntimeError(f"reconcile errors={result.errors}")
            await session.refresh(active_key)
            active_key.reissue_count_today = used + 1
            active_key.reissue_date = today
            active_key.last_reissued_at = _dt.utcnow()
            await session.commit()
            new_key = active_key
        except Exception as exc:
            logger.error("Bot key refresh failed for user %d: %s", db_user.id, type(exc).__name__, exc_info=True)
            await callback.message.answer(
                "❌ Не удалось безопасно обновить профиль. Рабочий ключ не удалён. "
                "Попробуйте позже или обратитесь в поддержку.",
                parse_mode="HTML",
            )
            return

        try:
            access_url = get_client_access_url(new_key)
        except Exception as exc:
            logger.error("Cannot format reissued VLESS link: %s", type(exc).__name__)
            await callback.message.answer(
                "Ключ обновлён, но ссылка не прошла проверку формата. "
                "Не импортируйте пустой профиль; обратитесь в поддержку."
            )
            return

    remaining_after = _MAX_DAILY_REISSUES - (used + 1)

    await callback.message.answer(  # type: ignore[union-attr]
        f"✅ <b>Ключ успешно обновлён!</b>\n\n"
        f"🔑 <b>Ваш актуальный ключ доступа:</b>\n\n"
        f"<code>{escape(access_url)}</code>\n\n"
        f"📱 Удалите старый ключ и добавьте новый:\n"
        f"• <b>Android:</b> Hiddify, V2RayNG\n"
        f"• <b>iPhone:</b> Streisand, FoXray, V2Box\n"
        f"• <b>Windows/macOS:</b> Hiddify\n"
        f"• <b>Linux:</b> Hiddify, v2rayA\n\n"
        f"📊 Осталось обновлений сегодня: <b>{remaining_after}/{_MAX_DAILY_REISSUES}</b>",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "reissue_cancel")
async def on_reissue_cancel(callback: CallbackQuery) -> None:
    await callback.answer("Отменено.")
    try:
        await callback.message.delete()  # type: ignore[union-attr]
    except Exception:
        pass
