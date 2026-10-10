"""Explicit production stabilization for dedicated VPN nodes.

Control-plane VPS is never used as a VPN node. This command is operator-only:
  docker compose exec -T admin python -m app.stabilize_vpn_fleet --apply

It preserves client UUIDs and Reality keys.
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.database import async_session_maker
from app.enums import VpnServerStatus
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _server_protocol
from app.services.vpn_reconcile import reconcile_marzban_server


async def run(apply: bool) -> int:
    failures = 0
    healthy = 0
    async with async_session_maker() as session:
        # The main VPS is control-plane only. Any unfinished temporary record
        # must never appear to customers or receive paid subscriptions.
        primaries = (await session.execute(
            select(VpnServer).where(VpnServer.provider == "private-line-primary")
        )).scalars().all()
        for primary in primaries:
            primary.is_selectable = False
            primary.status = VpnServerStatus.disabled
            print(f"DISABLED: temporary control-plane VPN record #{primary.id}")

        servers = (await session.execute(
            select(VpnServer)
            .where(VpnServer.provider != "private-line-primary")
            .order_by(VpnServer.id.asc())
        )).scalars().all()

        marzban = [s for s in servers if _server_protocol(s) == "marzban"]
        if not marzban:
            print("FAIL: no dedicated Marzban VPN node registered")
            await session.rollback()
            return 2

        for server in marzban:
            print(f"=== NODE #{server.id}: {server.name} ===")
            try:
                result = await reconcile_marzban_server(
                    session, server.id, apply=apply
                )
                print(
                    "RESULT: "
                    f"keys={result.checked_keys} "
                    f"hosts_fixed={result.repaired_hosts} "
                    f"users_fixed={result.repaired_users} "
                    f"urls_fixed={result.repaired_urls} "
                    f"errors={result.errors} "
                    f"core_resync={result.core_restarted}"
                )
                if result.errors:
                    failures += 1
                    server.is_selectable = False
                    server.last_error = f"VPN reconciliation errors: {result.errors}"
                else:
                    healthy += 1
            except Exception as exc:
                failures += 1
                server.is_selectable = False
                server.last_error = f"{type(exc).__name__}: {str(exc)[:430]}"
                print(f"FAIL: {type(exc).__name__}: {str(exc)[:180]}")

        if apply:
            await session.commit()
        else:
            await session.rollback()

    if healthy == 0:
        print("FATAL: no verified dedicated VPN nodes")
        return 2
    if failures:
        print(f"FAIL: {failures} node(s) require attention; healthy={healthy}")
        return 2
    print(f"PASS: {healthy} dedicated VPN node(s) reconciled without credential rotation")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.apply)))
