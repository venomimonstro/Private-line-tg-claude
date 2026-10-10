"""Compatibility wrapper for one-subscription preserve-first reconciliation.

No independent Marzban mutation logic lives here. UUIDs and Reality keys are
owned by app.services.vpn_reconcile.
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.database import async_session_maker
from app.enums import VpnKeyStatus
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _server_protocol
from app.services.vpn_reconcile import reconcile_marzban_server


async def run(sub_id: int, apply: bool) -> int:
    async with async_session_maker() as db:
        rows = (await db.execute(
            select(VpnKey, VpnServer)
            .join(VpnServer, VpnServer.id == VpnKey.server_id)
            .where(
                VpnKey.subscription_id == sub_id,
                VpnKey.status == VpnKeyStatus.active,
            )
            .order_by(VpnKey.server_id.asc())
        )).all()
        rows = [
            (key, server) for key, server in rows
            if server.provider != "private-line-primary"
            and _server_protocol(server) == "marzban"
        ]
        if not rows:
            print("STOP: no active dedicated Marzban key for subscription")
            return 2

        failures = 0
        seen: set[int] = set()
        for _key, server in rows:
            if server.id in seen:
                continue
            seen.add(server.id)
            try:
                result = await reconcile_marzban_server(
                    db,
                    server.id,
                    apply=apply,
                    only_subscription_id=sub_id,
                )
                print(
                    f"server={server.id} checked={result.checked_keys} "
                    f"hosts_fixed={result.repaired_hosts} "
                    f"users_fixed={result.repaired_users} "
                    f"urls_fixed={result.repaired_urls} errors={result.errors}"
                )
                failures += result.errors
            except Exception as exc:
                print(f"FAIL: server={server.id} {type(exc).__name__}: {str(exc)[:160]}")
                failures += 1

        if apply and failures == 0:
            await db.commit()
        else:
            await db.rollback()

        if failures:
            print("FAIL: reconciliation did not complete cleanly")
            return 2
        print("PASS: subscription reconciled without credential rotation")
        print(f"NEXT: bash scripts/probe_vless_e2e.sh {sub_id}")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--subscription", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.subscription <= 0:
        parser.error("subscription must be positive")
    raise SystemExit(asyncio.run(run(args.subscription, args.apply)))
