from __future__ import annotations

import io
import logging

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery

from app.models.user import User
from app.services.vpn_key import get_client_access_url
from bot.handlers.access import _get_active_subscription

router = Router(name="qr_key")
logger = logging.getLogger(__name__)


@router.callback_query(F.data == "get_key_qr")
async def on_get_key_qr(callback: CallbackQuery, db_user: User) -> None:
    _sub, active_keys, _tariff = await _get_active_subscription(db_user.id)
    if not active_keys:
        await callback.answer("Активный ключ на рабочей VPN-ноде не найден.", show_alert=True)
        return
    # QR is intentionally one profile at a time. The regular key/server menu
    # lets a multi-node user choose another key explicitly.
    active_key = active_keys[0]
    try:
        access_url = get_client_access_url(active_key)
    except Exception:
        await callback.answer("Ошибка проверки VPN-профиля.", show_alert=True)
        return

    try:
        import qrcode  # type: ignore[import-untyped]
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=4,
        )
        qr.add_data(access_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        await callback.message.answer_photo(
            photo=BufferedInputFile(buf.read(), filename="private-line-vpn.png"),
            caption=(
                "📷 <b>Private Line VPN</b>\n\n"
                "VLESS Reality: V2RayNG, Hiddify, Streisand или V2Box."
            ),
            parse_mode="HTML",
        )
    except ImportError:
        from html import escape
        await callback.message.answer(
            f"🔑 <b>Private Line VPN</b>\n\n<code>{escape(access_url)}</code>",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("QR generation failed: %s", type(exc).__name__)
        await callback.answer("Не удалось создать QR-код.", show_alert=True)
        return
    await callback.answer()
