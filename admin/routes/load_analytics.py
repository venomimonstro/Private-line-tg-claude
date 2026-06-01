from __future__ import annotations

import shutil
from datetime import datetime, timedelta

import psutil
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from admin.dependencies import get_current_admin
from app.config import settings
from app.database import get_async_session
from app.enums import SubscriptionStatus, VpnKeyStatus, VpnServerStatus
from app.models.subscription import Subscription
from app.models.user import User
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer

router = APIRouter(prefix="/load")
templates = Jinja2Templates(directory="admin/templates")


@router.get("", response_class=HTMLResponse)
async def load_analytics(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_admin),
) -> HTMLResponse:
    cpu_pct = psutil.cpu_percent()  # non-blocking, uses cached value
    ram = psutil.virtual_memory()
    disk = shutil.disk_usage(settings.APP_DIR)
    disk_used_pct = disk.used / disk.total * 100
    free_disk_gb = disk.free / (1024 ** 3)

    servers_result = await session.execute(select(VpnServer).order_by(VpnServer.id))
    servers = servers_result.scalars().all()

    active_keys_result = await session.execute(
        select(func.count(VpnKey.id)).where(VpnKey.status == VpnKeyStatus.active)
    )
    active_keys = active_keys_result.scalar_one() or 0

    # Top users by traffic — with user info
    top_keys_result = await session.execute(
        select(VpnKey)
        .options(selectinload(VpnKey.user))
        .where(VpnKey.traffic_used_gb > 0)
        .order_by(VpnKey.traffic_used_gb.desc())
        .limit(15)
    )
    top_users = top_keys_result.scalars().all()

    total_traffic_result = await session.execute(
        select(func.coalesce(func.sum(VpnKey.traffic_used_gb), 0))
    )
    total_traffic_gb = float(total_traffic_result.scalar_one() or 0)

    # Recently active users (last 60 minutes)
    hour_ago = datetime.utcnow() - timedelta(hours=1)
    recent_result = await session.execute(
        select(User)
        .where(User.last_activity_at >= hour_ago)
        .order_by(User.last_activity_at.desc())
        .limit(30)
    )
    recent_users = recent_result.scalars().all()

    # Users active in last 24h count
    day_ago = datetime.utcnow() - timedelta(hours=24)
    active_24h = (await session.execute(
        select(func.count(User.id)).where(User.last_activity_at >= day_ago)
    )).scalar_one() or 0

    # Users with most total traffic — return plain dicts to avoid lazy-load issues
    heavy_result = await session.execute(
        select(
            User.id, User.first_name, User.username, User.telegram_id,
            func.coalesce(func.sum(VpnKey.traffic_used_gb), 0).label("total_gb"),
        )
        .join(VpnKey, VpnKey.user_id == User.id)
        .where(VpnKey.status == VpnKeyStatus.active)
        .group_by(User.id, User.first_name, User.username, User.telegram_id)
        .order_by(func.sum(VpnKey.traffic_used_gb).desc())
        .limit(10)
    )
    heavy_users = [
        {
            "id": row.id, "first_name": row.first_name,
            "username": row.username, "telegram_id": row.telegram_id,
            "total_gb": float(row.total_gb or 0),
        }
        for row in heavy_result.all()
    ]

    warnings = []
    if cpu_pct > 80:
        warnings.append(f"CPU загружен на {cpu_pct:.0f}%")
    if ram.percent > 85:
        warnings.append(f"RAM загружена на {ram.percent:.0f}%")
    if disk_used_pct > 80:
        warnings.append(f"Диск заполнен на {disk_used_pct:.0f}%")
    for server in servers:
        if server.status == VpnServerStatus.unreachable:
            warnings.append(f"VPN-сервер {server.name} недоступен")
        if server.max_users > 0 and server.current_users >= server.max_users * 0.9:
            warnings.append(f"Сервер {server.name}: использовано {server.current_users}/{server.max_users} мест")

    context = {
        "request": request, "admin": admin, "page_title": "Аналитика нагрузки",
        "cpu_pct": cpu_pct, "ram_pct": ram.percent,
        "ram_used_mb": round(ram.used / (1024 ** 2), 1),
        "ram_total_mb": round(ram.total / (1024 ** 2), 1),
        "disk_used_pct": round(disk_used_pct, 1), "free_disk_gb": round(free_disk_gb, 2),
        "servers": servers, "active_keys": active_keys,
        "top_users": top_users, "total_traffic_gb": round(total_traffic_gb, 2),
        "recent_users": recent_users, "active_24h": active_24h,
        "heavy_users": heavy_users,
        "warnings": warnings,
    }
    return templates.TemplateResponse("load_analytics.html", context)
