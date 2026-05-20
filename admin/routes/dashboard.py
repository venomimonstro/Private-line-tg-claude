from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin
from app.database import get_async_session
from app.enums import BackupStatus, HealthStatus, PaymentStatus, SubscriptionStatus
from app.models.backup import Backup
from app.models.health_check import SystemHealthCheck
from app.models.payment import Payment
from app.models.subscription import Subscription
from app.models.user import User
from app.models.vpn_key import VpnKey

router = APIRouter()
templates = Jinja2Templates(directory="admin/templates")


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    active_subs_result = await session.execute(
        select(func.count(Subscription.id)).where(
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon])
        )
    )
    active_subs = active_subs_result.scalar_one() or 0

    new_users_today_result = await session.execute(
        select(func.count(User.id)).where(User.created_at >= today)
    )
    new_users_today = new_users_today_result.scalar_one() or 0

    revenue_today_result = await session.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.status == PaymentStatus.paid,
            Payment.confirmed_at >= today,
        )
    )
    revenue_today = float(revenue_today_result.scalar_one() or 0)

    active_keys_result = await session.execute(
        select(func.count(VpnKey.id)).where(VpnKey.status == "active")
    )
    active_keys = active_keys_result.scalar_one() or 0

    health_result = await session.execute(
        select(SystemHealthCheck).order_by(SystemHealthCheck.checked_at.desc()).limit(1)
    )
    health = health_result.scalar_one_or_none()

    last_backup_result = await session.execute(
        select(Backup).where(Backup.status == BackupStatus.success).order_by(Backup.created_at.desc()).limit(1)
    )
    last_backup = last_backup_result.scalar_one_or_none()

    context = {
        "request": request,
        "admin": admin,
        "active_subs": active_subs,
        "new_users_today": new_users_today,
        "revenue_today": revenue_today,
        "active_keys": active_keys,
        "health": health,
        "last_backup": last_backup,
        "app_version": "1.0.0",
        "page_title": "Дашборд",
    }
    return templates.TemplateResponse("dashboard.html", context)
