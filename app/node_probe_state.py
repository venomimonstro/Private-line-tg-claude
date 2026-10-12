"""Persist the result of an external VPN tunnel probe.

This module is intentionally tiny: shell acceptance gates perform the real
Xray/HTTPS probe, while this command only changes admission state. Existing
customer credentials are never deleted or rotated.

Failure => stop assigning NEW subscriptions to the node, but keep the node
record available so existing customers can still retrieve their already-issued
profiles while an operator repairs it.
Success => publish the node again only after the external acceptance gate has
completed.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

from app.database import async_session_maker
from app.enums import VpnServerStatus
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _server_protocol


async def run(server_id: int, state: str, reason: str | None) -> int:
    async with async_session_maker() as session:
        server = await session.get(VpnServer, server_id)
        if (
            server is None
            or server.provider == "private-line-primary"
            or _server_protocol(server) != "marzban"
        ):
            print("STOP: dedicated Marzban node not found")
            return 2

        if state == "failure":
            server.is_selectable = False
            server.status = VpnServerStatus.unreachable
            server.failure_count = max(1, int(server.failure_count or 0) + 1)
            server.last_error = (reason or "real VPN tunnel probe failed")[:500]
            server.last_error_at = datetime.utcnow()
            await session.commit()
            print(
                f"QUARANTINED: node {server.id}; new VPN assignments disabled; "
                "existing credentials preserved"
            )
            return 0

        if state == "success":
            server.status = VpnServerStatus.active
            server.is_selectable = True
            server.failure_count = 0
            server.last_error = None
            server.last_error_at = None
            await session.commit()
            print(f"HEALTHY: node {server.id}; external VPN tunnel acceptance passed")
            return 0

        print("STOP: invalid probe state")
        return 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", type=int, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--success", action="store_true")
    group.add_argument("--failure", action="store_true")
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    if args.server <= 0:
        parser.error("--server must be positive")
    state = "success" if args.success else "failure"
    raise SystemExit(asyncio.run(run(args.server, state, args.reason)))
