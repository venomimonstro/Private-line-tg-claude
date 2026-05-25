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


async def get_provider_db(name: str, session) -> PaymentProviderBase:
    """Build provider using credentials from AppSetting DB first, env var fallback."""
    from app.config import settings
    from app.models.app_setting import AppSetting
    from sqlalchemy import select

    cred_keys = [
        "YOOKASSA_SHOP_ID", "YOOKASSA_SECRET_KEY",
        "YOOMONEY_WALLET", "YOOMONEY_NOTIFICATION_SECRET", "YOOMONEY_ACCESS_TOKEN",
        "FREEKASSA_MERCHANT_ID", "FREEKASSA_SECRET_1", "FREEKASSA_SECRET_2", "FREEKASSA_API_KEY", "FREEKASSA_CURRENCY_ID",
        "LAVA_PROJECT_ID", "LAVA_SECRET_KEY",
        "ROBOKASSA_MERCHANT_LOGIN", "ROBOKASSA_PASSWORD_1", "ROBOKASSA_PASSWORD_2",
        "PRODAMUS_API_URL", "PRODAMUS_WEBHOOK_SECRET",
        "CRYPTO_BOT_TOKEN",
    ]
    result = await session.execute(
        select(AppSetting).where(AppSetting.key.in_(cred_keys))
    )
    db_vals = {row.key: (row.value or "") for row in result.scalars().all()}

    def g(key: str) -> str:
        return db_vals.get(key) or getattr(settings, key, "")

    return _build_with_creds(name, g)


def _build_with_creds(name: str, g) -> PaymentProviderBase:
    if name == "manual":
        from app.payments.manual import ManualPaymentProvider
        return ManualPaymentProvider()

    if name == "yoomoney":
        from app.payments.providers.yoomoney import YooMoneyProvider
        return YooMoneyProvider(g("YOOMONEY_WALLET"), g("YOOMONEY_NOTIFICATION_SECRET"), g("YOOMONEY_ACCESS_TOKEN"))

    if name == "freekassa":
        from app.payments.providers.freekassa import FreeKassaProvider
        return FreeKassaProvider(
            merchant_id=g("FREEKASSA_MERCHANT_ID"),
            secret_1=g("FREEKASSA_SECRET_1"),
            secret_2=g("FREEKASSA_SECRET_2"),
            currency_id=g("FREEKASSA_CURRENCY_ID"),
        )

    if name == "lava":
        from app.payments.providers.lava import LavaProvider
        return LavaProvider(g("LAVA_PROJECT_ID"), g("LAVA_SECRET_KEY"))

    if name == "yookassa":
        from app.payments.providers.yookassa import YooKassaProvider
        return YooKassaProvider(g("YOOKASSA_SHOP_ID"), g("YOOKASSA_SECRET_KEY"))

    if name == "robokassa":
        from app.payments.providers.robokassa import RobokassaProvider
        return RobokassaProvider(g("ROBOKASSA_MERCHANT_LOGIN"), g("ROBOKASSA_PASSWORD_1"), g("ROBOKASSA_PASSWORD_2"))

    if name == "prodamus":
        from app.payments.providers.prodamus import ProdamusProvider
        return ProdamusProvider(g("PRODAMUS_API_URL"), g("PRODAMUS_WEBHOOK_SECRET"))

    if name == "stars":
        from app.payments.providers.stars import TelegramStarsProvider
        return TelegramStarsProvider()

    if name == "crypto":
        from app.payments.providers.crypto import CryptoBotProvider
        return CryptoBotProvider(g("CRYPTO_BOT_TOKEN"))

    raise ValueError(f"Unknown payment provider: {name}")


def _build(name: str) -> PaymentProviderBase:
    from app.config import settings
    return _build_with_creds(name, lambda key: getattr(settings, key, ""))


async def close_all() -> None:
    for provider in list(_cache.values()):
        await provider.aclose()
    _cache.clear()
