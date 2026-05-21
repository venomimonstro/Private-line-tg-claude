from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot_instance import set_bot
from app.config import settings
from bot.handlers import access, geo_switch, help, mykey, payment, plans, referral, refund, rules, start, support
from bot.handlers import onboarding, payment_status, qr_key, stars_payment
from bot.middleware.user_activity import UserActivityMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    set_bot(bot)

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    dp.message.middleware(UserActivityMiddleware())
    dp.callback_query.middleware(UserActivityMiddleware())

    dp.include_router(start.router)
    dp.include_router(rules.router)
    dp.include_router(plans.router)
    dp.include_router(access.router)
    dp.include_router(mykey.router)
    dp.include_router(geo_switch.router)
    dp.include_router(payment.router)
    dp.include_router(referral.router)
    dp.include_router(refund.router)
    dp.include_router(support.router)
    dp.include_router(qr_key.router)
    dp.include_router(help.router)
    dp.include_router(stars_payment.router)
    dp.include_router(onboarding.router)
    dp.include_router(payment_status.router)

    async def shutdown(bot: Bot) -> None:
        logger.info("Shutting down...")
        await bot.session.close()
        from app.payments.registry import close_all
        await close_all()

    logger.info("Starting Private Line bot...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await shutdown(bot)


if __name__ == "__main__":
    asyncio.run(main())
