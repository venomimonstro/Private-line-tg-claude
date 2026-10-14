"""Print recoverable dedicated VLESS node IDs for operator acceptance gates.

Quarantined/unreachable nodes are intentionally included so a successful real
E2E gate can recover them. Explicitly disabled nodes remain excluded.
"""
from __future__ import annotations

import asyncio
from sqlalchemy import select

from app.database import async_session_maker
from app.enums import VpnServerStatus
from app.models.vpn_server import VpnServer
from app.services.vpn_eligibility import is_vless_node


async def main() -> int:
    async with async_session_maker() as db:
        rows = (await db.execute(
            select(VpnServer)
            .where(
                VpnServer.status != VpnServerStatus.disabled,
                VpnServer.provider != "private-line-primary",
            )
            .order_by(VpnServer.id.asc())
        )).scalars().all()
        rows = [r for r in rows if is_vless_node(r)]
        if not rows:
            return 2
        for row in rows:
            print(row.id)
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
