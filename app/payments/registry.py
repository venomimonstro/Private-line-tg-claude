from __future__ import annotations

from app.payments.provider_base import PaymentProviderBase

_cache: dict[str, PaymentProviderBase] = {}


def get_provider(name: str | None = None) -> PaymentProviderBase:
    from app.config import settings
    name = name or settings.PAYMENT_PROVIDER
    if name in _cache:
        return _cache[name]
    provider = _build(name)
    _cache[name] = provider
    return provider


def _build(name: str) -> PaymentProviderBase:
    from app.config import settings

    if name == "manual":
        from app.payments.manual import ManualPaymentProvider
        return ManualPaymentProvider()

    if name == "yoomoney":
        from app.payments.providers.yoomoney import YooMoneyProvider
        return YooMoneyProvider(settings.YOOMONEY_WALLET, settings.YOOMONEY_NOTIFICATION_SECRET)

    if name == "freekassa":
        from app.payments.providers.freekassa import FreeKassaProvider
        return FreeKassaProvider(
            settings.FREEKASSA_MERCHANT_ID,
            settings.FREEKASSA_SECRET_1,
            settings.FREEKASSA_SECRET_2,
        )

    if name == "lava":
        from app.payments.providers.lava import LavaProvider
        return LavaProvider(settings.LAVA_PROJECT_ID, settings.LAVA_SECRET_KEY)

    if name == "yookassa":
        from app.payments.providers.yookassa import YooKassaProvider
        return YooKassaProvider(settings.YOOKASSA_SHOP_ID, settings.YOOKASSA_SECRET_KEY)

    if name == "robokassa":
        from app.payments.providers.robokassa import RobokassaProvider
        return RobokassaProvider(
            settings.ROBOKASSA_MERCHANT_LOGIN,
            settings.ROBOKASSA_PASSWORD_1,
            settings.ROBOKASSA_PASSWORD_2,
        )

    if name == "prodamus":
        from app.payments.providers.prodamus import ProdamusProvider
        return ProdamusProvider(settings.PRODAMUS_API_URL, settings.PRODAMUS_WEBHOOK_SECRET)

    if name == "stars":
        from app.payments.providers.stars import TelegramStarsProvider
        return TelegramStarsProvider()

    if name == "crypto":
        from app.payments.providers.crypto import CryptoBotProvider
        return CryptoBotProvider(settings.CRYPTO_BOT_TOKEN)

    raise ValueError(f"Unknown payment provider: {name}")
