from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    BOT_TOKEN: str
    ADMIN_CHAT_ID: int

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./private_line.db"

    # Security
    ENCRYPTION_KEY: str
    SECRET_KEY: str

    # Application
    APP_VERSION: str = "1.0.0"
    BACKUP_DIR: str = "./backups"
    APP_DIR: str = "."
    MAINTENANCE_MODE: bool = False

    # Admin session
    SESSION_TIMEOUT_MINUTES: int = 60
    MAX_LOGIN_ATTEMPTS: int = 5
    LOGIN_LOCKOUT_MINUTES: int = 30

    # Payment (general)
    PAYMENT_INSTRUCTIONS: str = "Свяжитесь с поддержкой для получения реквизитов оплаты."
    PAYMENT_PROVIDER: str = "manual"
    BOT_USERNAME: str = ""
    WEBHOOK_BASE_URL: str = ""
    STARS_PER_RUB: float = 0.5

    # YooMoney
    YOOMONEY_WALLET: str = ""
    YOOMONEY_NOTIFICATION_SECRET: str = ""

    # FreeKassa
    FREEKASSA_MERCHANT_ID: str = ""
    FREEKASSA_SECRET_1: str = ""
    FREEKASSA_SECRET_2: str = ""

    # LAVA
    LAVA_PROJECT_ID: str = ""
    LAVA_SECRET_KEY: str = ""

    # YooKassa
    YOOKASSA_SHOP_ID: str = ""
    YOOKASSA_SECRET_KEY: str = ""

    # Robokassa
    ROBOKASSA_MERCHANT_LOGIN: str = ""
    ROBOKASSA_PASSWORD_1: str = ""
    ROBOKASSA_PASSWORD_2: str = ""

    # Prodamus
    PRODAMUS_API_URL: str = ""
    PRODAMUS_WEBHOOK_SECRET: str = ""

    # CryptoBot
    CRYPTO_BOT_TOKEN: str = ""

    # Optional IP allowlist for admin panel
    ADMIN_ALLOWED_IPS: list[str] = []

    # Backup bot for notifications when main bot is blocked
    BACKUP_BOT_TOKEN: str = ""
    # Custom Telegram API URL (for anti-blocking proxy)
    TELEGRAM_API_URL: str = ""

    # Refund settings
    REFUND_WINDOW_DAYS: int = 7
    REFUND_MAX_TRAFFIC_MB: float = 100.0

    @field_validator("ADMIN_ALLOWED_IPS", mode="before")
    @classmethod
    def parse_allowed_ips(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [ip.strip() for ip in v.split(",") if ip.strip()]
        return v  # type: ignore[return-value]


settings = Settings()  # type: ignore[call-arg]
