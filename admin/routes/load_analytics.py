from __future__ import annotations

import shutil

import psutil
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.dependencies import get_current_admin
from app.config import settings
from app.database import get_async_session
from app.enums import VpnKeyStatus, VpnServerStatus
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
    cpu_pct = psutil.cpu_percent(interval=1)
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

    top_users_result = await session.execute(
        select(VpnKey).where(VpnKey.status == VpnKeyStatus.active)
        .order_by(VpnKey.traffic_used_gb.desc()).limit(10)
    )
    top_users = top_users_result.scalars().all()

    total_traffic_result = await session.execute(
        select(func.coalesce(func.sum(VpnKey.traffic_used_gb), 0))
    )
    total_traffic_gb = float(total_traffic_result.scalar_one() or 0)

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
        "disk_used_pct": round(disk_used_pct, 1), "free_disk_gb": round(free_disk_gb, 2),
        "servers": servers, "active_keys": active_keys,
        "top_users": top_users, "total_traffic_gb": round(total_traffic_gb, 2),
        "warnings": warnings,
    }
    return templates.TemplateResponse("load_analytics.html", context)
