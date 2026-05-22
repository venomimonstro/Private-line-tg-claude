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
    {"section": "YooKassa",  "key": "YOOKASSA_SHOP_ID",             "label": "ID магазина",      "type": "text",     "placeholder": "123456"},
    {"section": "YooKassa",  "key": "YOOKASSA_SECRET_KEY",          "label": "Секретный ключ",   "type": "password", "placeholder": "test_..."},
    {"section": "CryptoBot", "key": "CRYPTO_BOT_TOKEN",             "label": "Токен CryptoBot",  "type": "password", "placeholder": "1234:AAA..."},
    {"section": "Общие",     "key": "PAYMENT_CURRENCY",             "label": "Валюта",           "type": "text",     "placeholder": "RUB"},
    {"section": "Общие",     "key": "PAYMENT_DESCRIPTION_TEMPLATE", "label": "Шаблон описания",  "type": "text",     "placeholder": "Подписка Private Line — {tariff}"},
]

_SECTIONS = ["YooKassa", "CryptoBot", "Общие"]


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
    return templates.TemplateResponse("integrations.html", {
        "request": request, "admin": admin, "page_title": "Интеграции",
        "sections": _SECTIONS, "settings_meta": _SETTINGS_META,
        "values": values, "saved": False,
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
        val = str(form.get(key, "")).strip()
        existing = await session.get(AppSetting, key)
        if existing:
            existing.value = val or existing.value
            existing.updated_at = now
            existing.updated_by = admin.id
        elif val:
            session.add(AppSetting(
                key=key, value=val, description=meta["label"],
                updated_at=now, updated_by=admin.id,
            ))
    await session.commit()
    values = await _load_settings(session)
    return templates.TemplateResponse("integrations.html", {
        "request": request, "admin": admin, "page_title": "Интеграции",
        "sections": _SECTIONS, "settings_meta": _SETTINGS_META,
        "values": values, "saved": True,
    })
