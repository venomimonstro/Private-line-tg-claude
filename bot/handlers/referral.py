from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message

from app.models.user import User

router = Router(name="referral")


@router.message(F.text == "👥 Пригласить друга")
async def invite_friend(message: Message, db_user: User) -> None:
    bot_info = await message.bot.get_me()  # type: ignore[union-attr]
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{db_user.telegram_id}"

    await message.answer(
        f"👥 <b>Пригласите друга и получите +14 дней к подписке!</b>\n\n"
        f"Поделитесь вашей персональной ссылкой:\n"
        f"<code>{ref_link}</code>\n\n"
        f"Когда друг оформит первую подписку, вам автоматически добавятся 14 дней.",
        parse_mode="HTML",
    )
