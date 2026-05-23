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
    # FreeKassa
    {"section": "FreeKassa",       "key": "FREEKASSA_ENABLED",              "label": "Включён",                   "type": "toggle",   "placeholder": ""},
    {"section": "FreeKassa",       "key": "FREEKASSA_MERCHANT_ID",          "label": "ID магазина",               "type": "text",     "placeholder": "123456"},
    {"section": "FreeKassa",       "key": "FREEKASSA_SECRET_1",             "label": "Секрет 1 (для платежей)",   "type": "password", "placeholder": ""},
    {"section": "FreeKassa",       "key": "FREEKASSA_SECRET_2",             "label": "Секрет 2 (для уведомлений)","type": "password", "placeholder": ""},
    {"section": "FreeKassa",       "key": "FREEKASSA_API_KEY",              "label": "API-ключ (v2)",              "type": "password", "placeholder": ""},
    # LAVA
    {"section": "LAVA",            "key": "LAVA_ENABLED",                   "label": "Включён",                   "type": "toggle",   "placeholder": ""},
    {"section": "LAVA",            "key": "LAVA_PROJECT_ID",                "label": "ID проекта",                "type": "text",     "placeholder": ""},
    {"section": "LAVA",            "key": "LAVA_SECRET_KEY",                "label": "Секретный ключ",            "type": "password", "placeholder": ""},
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
    {"section": "Общие",           "key": "PAYMENT_PROVIDER",               "label": "Активный провайдер (код)",  "type": "text",     "placeholder": "yookassa"},
    {"section": "Общие",           "key": "PAYMENT_CURRENCY",               "label": "Валюта по умолчанию",       "type": "text",     "placeholder": "RUB"},
    {"section": "Общие",           "key": "PAYMENT_DESCRIPTION_TEMPLATE",   "label": "Шаблон описания платежа",   "type": "text",     "placeholder": "Подписка Private Line — {tariff}"},
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
    "FreeKassa":       ["FREEKASSA_MERCHANT_ID", "FREEKASSA_SECRET_2", "FREEKASSA_API_KEY"],
    "LAVA":            ["LAVA_PROJECT_ID", "LAVA_SECRET_KEY"],
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
    return {
        meta["key"]: db_vals.get(meta["key"]) or os.environ.get(meta["key"], "")
        for meta in _SETTINGS_META
    }


def _build_context(request, admin, values: dict, saved: bool) -> dict:
    provider_status = {s: _provider_status(s, values) for s in _SECTIONS}
    base_url = str(request.base_url).rstrip("/")
    return {
        "request": request, "admin": admin, "page_title": "Интеграции",
        "sections": _SECTIONS, "settings_meta": _SETTINGS_META,
        "values": values, "saved": saved, "provider_status": provider_status,
        "base_url": base_url,
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
                existing.value = val
            existing.updated_at = now
            existing.updated_by = admin.id
        elif val:
            session.add(AppSetting(
                key=key, value=val, description=meta["label"],
                updated_at=now, updated_by=admin.id,
            ))
    await session.commit()
    values = await _load_settings(session)
    return templates.TemplateResponse("integrations.html", _build_context(request, admin, values, True))
