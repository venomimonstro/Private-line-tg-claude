from __future__ import annotations

from html import escape
from app.services.node_selection import rank_nodes

import asyncio
import time
import urllib.parse
from typing import Optional

from aiogram import F, Router
from aiogram.types import Message

from app.database import async_session_maker
from app.enums import VpnKeyStatus, VpnServerStatus
from app.models.user import User
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from sqlalchemy import select
from sqlalchemy.orm import selectinload

router = Router(name="speed_test")


async def _tcp_latency(host: str, port: int, timeout: float = 3.0) -> Optional[float]:
    """Returns TCP connection latency in ms, or None if unreachable."""
    try:
        start = time.monotonic()
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        elapsed = (time.monotonic() - start) * 1000
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return elapsed
    except Exception:
        return None


def _parse_host_port(url: str) -> tuple[str, int] | None:
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        port = parsed.port
        if not host:
            return None
        if not port:
            port = 443 if parsed.scheme == "https" else 80
        return host, port
    except Exception:
        return None


def _latency_emoji(ms: float | None) -> str:
    if ms is None:
        return "🔴"
    if ms < 50:
        return "🟢"
    if ms < 150:
        return "🟡"
    return "🔴"


@router.message(F.text == "🚀 Проверить скорость")
async def speed_test(message: Message, db_user: User) -> None:
    msg = await message.answer("⏳ Проверяем подключение к серверам...")

    async with async_session_maker() as session:
        # Get user's active VPN key with server info
        key_result = await session.execute(
            select(VpnKey)
            .options(selectinload(VpnKey.server))
            .join(VpnServer, VpnServer.id == VpnKey.server_id)
            .where(
                VpnKey.user_id == db_user.id,
                VpnKey.status == VpnKeyStatus.active,
                VpnServer.status == VpnServerStatus.active,
                VpnServer.is_selectable.is_(True),
                VpnServer.provider != "private-line-primary",
            )
            .limit(1)
        )
        user_key = key_result.scalars().first()

        # Get all active servers
        servers_result = await session.execute(
            select(VpnServer).where(
                VpnServer.status == VpnServerStatus.active,
                VpnServer.is_selectable.is_(True),
                VpnServer.provider != "private-line-primary",
            )
            .order_by(VpnServer.sort_order if hasattr(VpnServer, "sort_order") else VpnServer.id)
        )
        servers = servers_result.scalars().all()

    if not servers:
        await msg.edit_text("❌ Нет активных серверов.")
        return

    results = await rank_nodes(servers)

    user_server_id = user_key.server_id if user_key else None

    lines = ["<b>🌐 Доступность VPN-нод с сервера Private Line</b>\n"]
    for server, ms in results:
        emoji = _latency_emoji(ms)
        ms_str = f"{ms:.0f} мс" if ms is not None else "недоступен"
        flag = server.flag_emoji or ""
        marker = " ← <b>Ваш</b>" if server.id == user_server_id else ""
        lines.append(
            f"{emoji} {flag} <b>{escape(server.name)}</b>{marker}\n"
            f"   Пинг: {ms_str}\n"
        )

    lines.append(
        "\n<i>Это техническая задержка основной сервер → VPN-порт ноды, а не скорость вашего телефона.</i>"
    )

    await msg.edit_text("\n".join(lines), parse_mode="HTML")
