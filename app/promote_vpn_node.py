"""Publish a staged dedicated VPN node after external smoke success.

This command intentionally cannot repair or rotate credentials. It performs one
last read-only integrity check and flips only is_selectable.
"""
from __future__ import annotations

import argparse
import asyncio

from app.database import async_session_maker
from app.enums import VpnServerStatus
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _server_protocol
from app.services.vpn_reconcile import reconcile_marzban_server


async def run(server_id: int) -> int:
    async with async_session_maker() as session:
        server = await session.get(VpnServer, server_id)
        if (
            server is None
            or server.provider == "private-line-primary"
            or _server_protocol(server) != "marzban"
            or server.status != VpnServerStatus.active
        ):
            print("STOP: server is not an active dedicated Marzban node")
            return 2

        result = await reconcile_marzban_server(session, server_id, apply=False)
        if result.errors:
            print(f"STOP: node integrity has {result.errors} error(s); not published")
            return 2

        server.is_selectable = True
        server.failure_count = 0
        server.last_error = None
        server.last_error_at = None
        await session.commit()
        print(f"PASS: VPN node {server_id} published for customer selection")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", type=int, required=True)
    args = parser.parse_args()
    if args.server <= 0:
        parser.error("--server must be positive")
    raise SystemExit(asyncio.run(run(args.server)))
