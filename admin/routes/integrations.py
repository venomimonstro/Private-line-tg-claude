from __future__ import annotations

import os
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import require_super_admin
from app.database import get_async_session
from app.models.app_setting import AppSetting
from app.models.user import User
from app.services.secret_settings import decode_setting_value, encode_setting_value

router = APIRouter(prefix="/integrations")
templates = Jinja2Templates(directory="admin/templates")

_SETTINGS_META: list[dict] = [
    # YooKassa
    {"section": "YooKassa",        "key": "YOOKASSA_ENABLED",               "label": "Включён",                   "type": "toggle",   "placeholder": ""},
    {"section": "YooKassa",        "key": "YOOKASSA_SHOP_ID",               "label": "ID магазина",               "type": "text",     "placeholder": "123456"},
    {"section": "YooKassa",        "key": "YOOKASSA_SECRET_KEY",            "label": "Секретный ключ",            "type": "password", "placeholder": "test_..."},
    # YooMoney
    {"section": "ЮMoney",          "key": "YOOMONEY_ENABLED",               "label": "Включён",                   "type": "toggle",   "placeholder": ""},
    {"section": "ЮMoney",          "key": "YOOMONEY_WALLET",                "label": "Номер кошелька",            "type": "text",     "placeholder": "4100118..."},
    {"section": "ЮMoney",          "key": "YOOMONEY_NOTIFICATION_SECRET",   "label": "Секрет уведомлений",        "type": "password", "placeholder": ""},
    {"section": "ЮMoney",          "key": "YOOMONEY_ACCESS_TOKEN",          "label": "OAuth-токен (авто-проверка)", "type": "password", "placeholder": ""},
    # FreeKassa
    {"section": "FreeKassa",       "key": "FREEKASSA_ENABLED",              "label": "Включён",                   "type": "toggle",   "placeholder": ""},
    {"section": "FreeKassa",       "key": "FREEKASSA_MERCHANT_ID",          "label": "ID магазина",               "type": "text",     "placeholder": "123456"},
    {"section": "FreeKassa",       "key": "FREEKASSA_SECRET_1",             "label": "Секрет 1 (для платежей)",   "type": "password", "placeholder": ""},
    {"section": "FreeKassa",       "key": "FREEKASSA_SECRET_2",             "label": "Секрет 2 (для уведомлений)","type": "password", "placeholder": ""},
    {"section": "FreeKassa",       "key": "FREEKASSA_API_KEY",              "label": "API-ключ (необязательно)",   "type": "password", "placeholder": ""},
    {"section": "FreeKassa",       "key": "FREEKASSA_CURRENCY_ID",          "label": "ID валюты (параметр i)",    "type": "text",     "placeholder": "643"},
    # LAVA
    {"section": "LAVA",            "key": "LAVA_ENABLED",                   "label": "Включён",                   "type": "toggle",   "placeholder": ""},
    {"section": "LAVA",            "key": "LAVA_PROJECT_ID",                "label": "ID проекта",                "type": "text",     "placeholder": ""},
    {"section": "LAVA",            "key": "LAVA_SECRET_KEY",                "label": "API секретный ключ",        "type": "password", "placeholder": ""},
    {"section": "LAVA",            "key": "LAVA_WEBHOOK_SECRET",             "label": "Доп. ключ webhook",         "type": "password", "placeholder": ""},
    # Robokassa
    {"section": "Robokassa",       "key": "ROBOKASSA_ENABLED",              "label": "Включён",                   "type": "toggle",   "placeholder": ""},
    {"section": "Robokassa",       "key": "ROBOKASSA_MERCHANT_LOGIN",       "label": "Логин магазина",            "type": "text",     "placeholder": "MyShop"},
    {"section": "Robokassa",       "key": "ROBOKASSA_PASSWORD_1",           "label": "Пароль 1",                  "type": "password", "placeholder": ""},
    {"section": "Robokassa",       "key": "ROBOKASSA_PASSWORD_2",           "label": "Пароль 2",                  "type": "password", "placeholder": ""},
    # Prodamus
    {"section": "Prodamus",        "key": "PRODAMUS_ENABLED",               "label": "Включён",                   "type": "toggle",   "placeholder": ""},
    {"section": "Prodamus",        "key": "PRODAMUS_API_URL",               "label": "URL платёжной формы",       "type": "text",     "placeholder": "https://myshop.payform.ru"},
    {"section": "Prodamus",        "key": "PRODAMUS_WEBHOOK_SECRET",        "label": "Секрет вебхука",            "type": "password", "placeholder": ""},
    # CryptoBot (TON / USDT / BTC и др.)
    {"section": "CryptoBot (TON)", "key": "CRYPTO_BOT_ENABLED",             "label": "Включён",                   "type": "toggle",   "placeholder": ""},
    {"section": "CryptoBot (TON)", "key": "CRYPTO_BOT_TOKEN",               "label": "Токен CryptoBot",           "type": "password", "placeholder": "1234:AAA..."},
    # Telegram Stars
    {"section": "Telegram Stars",  "key": "STARS_ENABLED",                  "label": "Включён",                   "type": "toggle",   "placeholder": ""},
    {"section": "Telegram Stars",  "key": "STARS_PER_RUB",                  "label": "Звёзд за 1 RUB",            "type": "text",     "placeholder": "0.5"},
    # Общие
    {"section": "Общие",           "key": "PAYMENT_PROVIDER",               "label": "Активный провайдер (код)",  "type": "text",     "placeholder": "freekassa"},
    {"section": "Общие",           "key": "PAYMENT_CURRENCY",               "label": "Валюта по умолчанию",       "type": "text",     "placeholder": "RUB"},
    {"section": "Общие",           "key": "PAYMENT_DESCRIPTION_TEMPLATE",   "label": "Шаблон описания платежа",   "type": "text",     "placeholder": "Подписка Private Line — {tariff}"},
    {"section": "Общие",           "key": "WEBHOOK_BASE_URL",               "label": "Базовый URL вебхуков",      "type": "text",     "placeholder": "http://155.212.224.153.nip.io"},
]

_SECTIONS = [
    "YooKassa", "ЮMoney", "FreeKassa", "LAVA",
    "Robokassa", "Prodamus", "CryptoBot (TON)", "Telegram Stars", "Общие",
]

_PROVIDER_ENABLED_KEYS: dict[str, str] = {
    "YooKassa":        "YOOKASSA_ENABLED",
    "ЮMoney":          "YOOMONEY_ENABLED",
    "FreeKassa":       "FREEKASSA_ENABLED",
    "LAVA":            "LAVA_ENABLED",
    "Robokassa":       "ROBOKASSA_ENABLED",
    "Prodamus":        "PRODAMUS_ENABLED",
    "CryptoBot (TON)": "CRYPTO_BOT_ENABLED",
    "Telegram Stars":  "STARS_ENABLED",
}

_PROVIDER_CREDENTIAL_KEYS: dict[str, list[str]] = {
    "YooKassa":        ["YOOKASSA_SHOP_ID", "YOOKASSA_SECRET_KEY"],
    "ЮMoney":          ["YOOMONEY_WALLET", "YOOMONEY_NOTIFICATION_SECRET"],
    "FreeKassa":       ["FREEKASSA_MERCHANT_ID", "FREEKASSA_SECRET_1", "FREEKASSA_SECRET_2"],
    "LAVA":            ["LAVA_PROJECT_ID", "LAVA_SECRET_KEY", "LAVA_WEBHOOK_SECRET"],
    "Robokassa":       ["ROBOKASSA_MERCHANT_LOGIN", "ROBOKASSA_PASSWORD_1", "ROBOKASSA_PASSWORD_2"],
    "Prodamus":        ["PRODAMUS_API_URL", "PRODAMUS_WEBHOOK_SECRET"],
    "CryptoBot (TON)": ["CRYPTO_BOT_TOKEN"],
    "Telegram Stars":  [],  # no credentials needed — built-in Telegram
}


def _provider_status(section: str, values: dict[str, str]) -> dict:
    enabled_key = _PROVIDER_ENABLED_KEYS.get(section)
    if enabled_key is None:
        return {}
    enabled = values.get(enabled_key, "").lower() in ("true", "1", "yes")
    cred_keys = _PROVIDER_CREDENTIAL_KEYS.get(section, [])
    has_credentials = not cred_keys or all(values.get(k, "").strip() for k in cred_keys)
    return {
        "enabled": enabled,
        "has_credentials": has_credentials,
        "ready": enabled and has_credentials,
    }


async def _load_settings(session: AsyncSession) -> dict[str, str]:
    result = await session.execute(select(AppSetting))
    db_vals = {row.key: (row.value or "") for row in result.scalars().all()}

    values: dict[str, str] = {}
    for meta in _SETTINGS_META:
        key = meta["key"]
        if key in db_vals and db_vals[key]:
            values[key] = decode_setting_value(key, db_vals[key])
        else:
            values[key] = os.environ.get(key, "")
    return values


def _build_context(request, admin, values: dict, saved: bool) -> dict:
    provider_status = {s: _provider_status(s, values) for s in _SECTIONS}
    # Use configured WEBHOOK_BASE_URL if set, otherwise fall back to request base URL
    webhook_base = (
        values.get("WEBHOOK_BASE_URL", "").rstrip("/")
        or str(request.base_url).rstrip("/")
    )
    return {
        "request": request, "admin": admin, "page_title": "Интеграции",
        "sections": _SECTIONS, "settings_meta": _SETTINGS_META,
        "values": values, "saved": saved, "provider_status": provider_status,
        "base_url": webhook_base,
    }


@router.get("", response_class=HTMLResponse)
async def integrations_page(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> HTMLResponse:
    values = await _load_settings(session)
    return templates.TemplateResponse("integrations.html", _build_context(request, admin, values, False))


@router.post("", response_class=HTMLResponse)
async def save_integrations(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> HTMLResponse:
    form = await request.form()
    now = datetime.utcnow()
    for meta in _SETTINGS_META:
        key = meta["key"]
        if meta["type"] == "toggle":
            val = "true" if form.get(key) else "false"
        else:
            val = str(form.get(key, "")).strip()
        existing = await session.get(AppSetting, key)
        if existing:
            if meta["type"] == "password" and not val:
                pass  # keep existing value when password field left empty
            else:
                existing.value = encode_setting_value(key, val)
            existing.updated_at = now
            existing.updated_by = admin.id
        elif val:
            session.add(AppSetting(
                key=key,
                value=encode_setting_value(key, val),
                description=meta["label"],
                updated_at=now,
                updated_by=admin.id,
            ))
    await session.commit()
    values = await _load_settings(session)
    return templates.TemplateResponse("integrations.html", _build_context(request, admin, values, True))
