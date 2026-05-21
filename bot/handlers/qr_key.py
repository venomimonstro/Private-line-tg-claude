from __future__ import annotations

import io
import logging

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery

from app.enums import SubscriptionStatus, VpnKeyStatus
from app.models.user import User

router = Router(name="qr_key")
logger = logging.getLogger(__name__)


@router.callback_query(F.data == "get_key_qr")
async def on_get_key_qr(callback: CallbackQuery, db_user: User) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.database import async_session_maker
    from app.models.subscription import Subscription
    from app.services.vpn_key import get_access_url

    async with async_session_maker() as session:
        result = await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.vpn_keys))
            .where(
                Subscription.user_id == db_user.id,
                Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
            )
            .order_by(Subscription.expires_at.desc())
        )
        sub = result.scalars().first()
        if sub is None:
            await callback.answer("Активная подписка не найдена.", show_alert=True)
            return

        active_key = next((k for k in sub.vpn_keys if k.status == VpnKeyStatus.active), None)
        if active_key is None:
            await callback.answer("Активный ключ не найден.", show_alert=True)
            return

        try:
            access_url = get_access_url(active_key)
        except Exception:
            await callback.answer("Ошибка получения ключа.", show_alert=True)
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

        await callback.message.answer_photo(  # type: ignore[union-attr]
            photo=BufferedInputFile(buf.read(), filename="vpn_key_qr.png"),
            caption=(
                "📷 <b>QR-код вашего ключа доступа</b>\n\n"
                "Отсканируйте в приложении Outline для быстрого подключения."
            ),
            parse_mode="HTML",
        )
    except ImportError:
        # qrcode not installed — send text fallback
        await callback.message.answer(  # type: ignore[union-attr]
            f"🔑 <b>Ваш ключ доступа:</b>\n\n<code>{access_url}</code>",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("QR generation failed: %s", exc)
        await callback.answer("Не удалось создать QR-код.", show_alert=True)
        return

    await callback.answer()
