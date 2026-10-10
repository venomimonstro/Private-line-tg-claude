"""Print verified dedicated Marzban node IDs, one per line, for shell gates."""
from __future__ import annotations

import asyncio
from sqlalchemy import select

from app.database import async_session_maker
from app.enums import VpnServerStatus
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _server_protocol


async def main() -> int:
    async with async_session_maker() as db:
        rows = (await db.execute(
            select(VpnServer)
            .where(
                VpnServer.status == VpnServerStatus.active,
                VpnServer.is_selectable.is_(True),
                VpnServer.provider == "marzban",
            )
            .order_by(VpnServer.id.asc())
        )).scalars().all()
        rows = [r for r in rows if _server_protocol(r) == "marzban"]
        if not rows:
            return 2
        for row in rows:
            print(row.id)
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
