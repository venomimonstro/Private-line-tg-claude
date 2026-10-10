"""Compatibility wrapper for Marzban host reconciliation.

Host repair is owned by app.services.vpn_reconcile so the same verified Reality
inbound/public endpoint rules are used everywhere.
"""
from __future__ import annotations

import argparse
import asyncio

from app.database import async_session_maker
from app.services.vpn_reconcile import reconcile_marzban_server


async def repair(server_id: int, apply: bool) -> int:
    async with async_session_maker() as session:
        try:
            result = await reconcile_marzban_server(
                session,
                server_id,
                apply=apply,
            )
        except Exception as exc:
            await session.rollback()
            print(f"FAIL: {type(exc).__name__}: {str(exc)[:160]}")
            return 2

        if apply and result.errors == 0:
            await session.commit()
        else:
            await session.rollback()

        print(
            f"server={server_id} checked={result.checked_keys} "
            f"hosts_fixed={result.repaired_hosts} "
            f"users_fixed={result.repaired_users} "
            f"urls_fixed={result.repaired_urls} errors={result.errors}"
        )
        if result.errors:
            return 2
        if not apply and result.repaired_hosts == 0:
            print("PASS: live Marzban host/inbound/key state is coherent")
        elif apply:
            print("PASS: preserve-first reconciliation completed")
            print("NEXT: run scripts/production_vpn_gate.sh <SUBSCRIPTION_ID>")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.server <= 0:
        parser.error("--server must be positive")
    raise SystemExit(asyncio.run(repair(args.server, args.apply)))
