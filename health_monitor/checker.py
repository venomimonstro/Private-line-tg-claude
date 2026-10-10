from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime, timedelta

import psutil
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.enums import BackupStatus, BackupType, HealthStatus, VpnServerStatus
from app.models.backup import Backup
from app.models.health_check import SystemHealthCheck
from app.models.subscription import Subscription
from app.models.vpn_server import VpnServer
from app.services.notification import notify_admin
from app.services.vpn_key import _make_client, _server_protocol

logger = logging.getLogger(__name__)

_COMPONENT_OK = "OK"
_COMPONENT_FAIL = "FAIL"

_CPU_WARN_THRESHOLD = 80.0
_RAM_WARN_THRESHOLD = 85.0
_DISK_WARN_THRESHOLD = 80.0


async def run_health_check(session: AsyncSession) -> SystemHealthCheck:
    details: dict = {}
    component_statuses: list[str] = []

    # Database
    database_status = _COMPONENT_FAIL
    try:
        await session.execute(text("SELECT 1"))
        database_status = _COMPONENT_OK
    except Exception as exc:
        details["database_error"] = str(exc)

    # Backend (we are running = OK)
    backend_status = _COMPONENT_OK

    # Bot check — attempt to reach Telegram API
    bot_status = _COMPONENT_FAIL
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"https://api.telegram.org/bot{settings.BOT_TOKEN}/getMe")
            if resp.status_code == 200:
                bot_status = _COMPONENT_OK
            else:
                details["bot_error"] = f"HTTP {resp.status_code}"
    except Exception as exc:
        details["bot_error"] = str(exc)

    # VPN fleet: every customer-selectable dedicated node must be coherent.
    vpn_api_status = _COMPONENT_FAIL
    vpn_server_result = await session.execute(
        select(VpnServer).where(
            VpnServer.status == VpnServerStatus.active,
            VpnServer.is_selectable.is_(True),
            VpnServer.provider == "marzban",
        )
    )
    vpn_servers = [
        s for s in vpn_server_result.scalars().all()
        if _server_protocol(s) == "marzban"
    ]
    if vpn_servers:
        failures: list[str] = []
        from app.services.vpn_reconcile import reconcile_marzban_server
        for vpn_server in vpn_servers:
            try:
                result = await reconcile_marzban_server(
                    session, vpn_server.id, apply=False
                )
                if result.errors:
                    failures.append(
                        f"node#{vpn_server.id}: integrity_errors={result.errors}"
                    )
            except Exception as exc:
                failures.append(
                    f"node#{vpn_server.id}: {type(exc).__name__}: {str(exc)[:100]}"
                )
        if failures:
            details["vpn_error"] = "; ".join(failures[:5])
            vpn_api_status = _COMPONENT_FAIL
        else:
            vpn_api_status = _COMPONENT_OK
    else:
        details["vpn_error"] = "No selectable dedicated VPN server configured"
        vpn_api_status = _COMPONENT_FAIL

    # DNS filter — check if a dns-resolving process or systemd-resolved is active
    dns_filter_status = _COMPONENT_OK
    try:
        proc_names = {p.name() for p in psutil.process_iter(["name"])}
        if not any(n in proc_names for n in ("dnsmasq", "unbound", "pihole-FTL", "systemd-resolve")):
            dns_filter_status = "N/A"
    except Exception:
        dns_filter_status = "N/A"

    # Firewall — check iptables on Linux
    firewall_status = _COMPONENT_FAIL
    try:
        result = subprocess.run(
            ["iptables", "-L", "-n"],
            capture_output=True, text=True, timeout=5,
        )
        firewall_status = _COMPONENT_OK if result.returncode == 0 else _COMPONENT_FAIL
    except FileNotFoundError:
        firewall_status = "N/A"
    except Exception as exc:
        details["firewall_error"] = str(exc)

    # Backup status — check last backup within 24h
    backup_status = _COMPONENT_FAIL
    last_backup_result = await session.execute(
        select(Backup)
        .where(Backup.status == BackupStatus.success)
        .order_by(Backup.created_at.desc())
        .limit(1)
    )
    last_backup = last_backup_result.scalar_one_or_none()
    if last_backup and last_backup.created_at >= datetime.utcnow() - timedelta(hours=24):
        backup_status = _COMPONENT_OK
    elif last_backup:
        details["backup_age_hours"] = round(
            (datetime.utcnow() - last_backup.created_at).total_seconds() / 3600, 1
        )

    # Disk space
    disk = shutil.disk_usage(settings.APP_DIR)
    free_disk_gb = disk.free / (1024 ** 3)
    disk_used_pct = disk.used / disk.total * 100
    details["disk_used_percent"] = round(disk_used_pct, 1)
    details["free_disk_gb"] = round(free_disk_gb, 2)

    # CPU / RAM
    cpu_pct = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    details["cpu_percent"] = cpu_pct
    details["ram_percent"] = ram.percent
    details["ram_used_mb"] = round(ram.used / (1024 ** 2), 1)

    # Active subscriptions check
    try:
        await session.execute(select(Subscription).limit(1))
    except Exception as exc:
        details["subscriptions_error"] = str(exc)

    component_statuses = [database_status, backend_status, bot_status, vpn_api_status, backup_status]
    if any(s == _COMPONENT_FAIL for s in component_statuses):
        overall = HealthStatus.critical
    elif cpu_pct > _CPU_WARN_THRESHOLD or ram.percent > _RAM_WARN_THRESHOLD or disk_used_pct > _DISK_WARN_THRESHOLD:
        overall = HealthStatus.warning
    elif any(s == "N/A" for s in component_statuses):
        overall = HealthStatus.warning
    else:
        overall = HealthStatus.ok

    record = SystemHealthCheck(
        status=overall,
        bot_status=bot_status,
        backend_status=backend_status,
        database_status=database_status,
        vpn_api_status=vpn_api_status,
        dns_filter_status=dns_filter_status,
        firewall_status=firewall_status,
        backup_status=backup_status,
        free_disk_space=round(free_disk_gb, 2),
        details=details,
    )
    session.add(record)
    await session.flush()
    await session.commit()

    if overall == HealthStatus.critical:
        await notify_admin(
            f"🚨 Health-check CRITICAL\n"
            f"DB: {database_status} | Bot: {bot_status} | VPN: {vpn_api_status}\n"
            f"Backup: {backup_status} | Disk: {disk_used_pct:.0f}% | CPU: {cpu_pct:.0f}%"
        )
    elif overall == HealthStatus.warning:
        await notify_admin(
            f"⚠️ Health-check WARNING\n"
            f"CPU: {cpu_pct:.0f}% | RAM: {ram.percent:.0f}% | Disk: {disk_used_pct:.0f}%"
        )

    return record
