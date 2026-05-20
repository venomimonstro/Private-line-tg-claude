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

    # Payment
    PAYMENT_INSTRUCTIONS: str = "Свяжитесь с поддержкой для получения реквизитов оплаты."

    # Optional IP allowlist for admin panel
    ADMIN_ALLOWED_IPS: list[str] = []

    @field_validator("ADMIN_ALLOWED_IPS", mode="before")
    @classmethod
    def parse_allowed_ips(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [ip.strip() for ip in v.split(",") if ip.strip()]
        return v  # type: ignore[return-value]


settings = Settings()  # type: ignore[call-arg]
