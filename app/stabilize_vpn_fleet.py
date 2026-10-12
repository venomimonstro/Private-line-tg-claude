"""Explicit production stabilization for dedicated VPN nodes.

Control-plane VPS is never used as a VPN node. This command is operator-only:
  docker compose exec -T admin python -m app.stabilize_vpn_fleet --apply

It preserves client UUIDs and Reality keys.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

from sqlalchemy import func, select

from app.database import async_session_maker
from app.enums import VpnServerStatus
from app.models.vpn_server import VpnServer
from app.models.vpn_key import VpnKey
from app.models.pending_operation import PendingOperation, PendingOperationType
from app.enums import VpnKeyStatus
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

        marzban = [
            s for s in servers
            if _server_protocol(s) == "marzban"
        ]
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
                # current_users is a derived DB count; repair drift from retries.
                active_count = await session.scalar(
                    select(func.count(VpnKey.id)).where(
                        VpnKey.server_id == server.id,
                        VpnKey.status == VpnKeyStatus.active,
                    )
                )
                server.current_users = int(active_count or 0)
                # This command proves only structural/control-plane
                # coherence. It must not erase a real data-plane quarantine
                # reason; only app.node_probe_state --success may do that.
                healthy += 1
                if result.errors:
                    failures += 1
                    if server.status == VpnServerStatus.active:
                        server.last_error = (
                            f"VPN key reconciliation errors: {result.errors}"
                        )
                elif server.status == VpnServerStatus.active:
                    server.last_error = None
            except Exception as exc:
                failures += 1
                server.is_selectable = False
                server.last_error = f"{type(exc).__name__}: {str(exc)[:430]}"
                print(f"FAIL: {type(exc).__name__}: {str(exc)[:180]}")

        if apply:
            # Close stale create retries only when that exact subscription/node
            # already has one active key. No remote or credential mutation.
            pending = (await session.execute(
                select(PendingOperation).where(
                    PendingOperation.completed_at.is_(None),
                    PendingOperation.operation_type == PendingOperationType.create_key.value,
                )
            )).scalars().all()
            for op in pending:
                if op.subscription_id is None or op.server_id is None:
                    continue
                active = (await session.execute(
                    select(VpnKey).where(
                        VpnKey.subscription_id == op.subscription_id,
                        VpnKey.server_id == op.server_id,
                        VpnKey.status == VpnKeyStatus.active,
                    )
                )).scalars().all()
                if len(active) == 1:
                    op.vpn_key_id = active[0].id
                    op.completed_at = datetime.utcnow()
                    op.last_error = None
                    print(f"CLEANUP: stale create operation #{op.id} closed")
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
