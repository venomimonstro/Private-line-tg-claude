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
    {"section": "YooKassa",  "key": "YOOKASSA_ENABLED",             "label": "Включён",          "type": "toggle",   "placeholder": ""},
    {"section": "YooKassa",  "key": "YOOKASSA_SHOP_ID",             "label": "ID магазина",      "type": "text",     "placeholder": "123456"},
    {"section": "YooKassa",  "key": "YOOKASSA_SECRET_KEY",          "label": "Секретный ключ",   "type": "password", "placeholder": "test_..."},
    {"section": "CryptoBot", "key": "CRYPTO_BOT_ENABLED",           "label": "Включён",          "type": "toggle",   "placeholder": ""},
    {"section": "CryptoBot", "key": "CRYPTO_BOT_TOKEN",             "label": "Токен CryptoBot",  "type": "password", "placeholder": "1234:AAA..."},
    {"section": "Общие",     "key": "PAYMENT_CURRENCY",             "label": "Валюта",           "type": "text",     "placeholder": "RUB"},
    {"section": "Общие",     "key": "PAYMENT_DESCRIPTION_TEMPLATE", "label": "Шаблон описания",  "type": "text",     "placeholder": "Подписка Private Line — {tariff}"},
]

_SECTIONS = ["YooKassa", "CryptoBot", "Общие"]

_PROVIDER_ENABLED_KEYS = {
    "YooKassa": "YOOKASSA_ENABLED",
    "CryptoBot": "CRYPTO_BOT_ENABLED",
}

_PROVIDER_CREDENTIAL_KEYS: dict[str, list[str]] = {
    "YooKassa": ["YOOKASSA_SHOP_ID", "YOOKASSA_SECRET_KEY"],
    "CryptoBot": ["CRYPTO_BOT_TOKEN"],
}


def _provider_status(section: str, values: dict[str, str]) -> dict:
    """Return activation status info for a provider section."""
    enabled_key = _PROVIDER_ENABLED_KEYS.get(section)
    if enabled_key is None:
        return {}
    enabled = values.get(enabled_key, "").lower() in ("true", "1", "yes")
    cred_keys = _PROVIDER_CREDENTIAL_KEYS.get(section, [])
    has_credentials = all(values.get(k, "").strip() for k in cred_keys)
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


@router.get("", response_class=HTMLResponse)
async def integrations_page(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_super_admin),
) -> HTMLResponse:
    values = await _load_settings(session)
    provider_status = {s: _provider_status(s, values) for s in _SECTIONS}
    return templates.TemplateResponse("integrations.html", {
        "request": request, "admin": admin, "page_title": "Интеграции",
        "sections": _SECTIONS, "settings_meta": _SETTINGS_META,
        "values": values, "saved": False, "provider_status": provider_status,
    })


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
    provider_status = {s: _provider_status(s, values) for s in _SECTIONS}
    return templates.TemplateResponse("integrations.html", {
        "request": request, "admin": admin, "page_title": "Интеграции",
        "sections": _SECTIONS, "settings_meta": _SETTINGS_META,
        "values": values, "saved": True, "provider_status": provider_status,
    })
