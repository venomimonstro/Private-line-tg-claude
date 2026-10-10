"""Subscription VLESS diagnostic backed by the production reconciler.

Without --repair-url this is read-only. The legacy flag is retained for
operator compatibility but now invokes the full preserve-first reconciliation
for the subscription; it never rewrites only one URL in isolation.
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


async def main(sub_id: int, repair: bool) -> int:
    async with async_session_maker() as session:
        rows = (await session.execute(
            select(VpnKey, VpnServer)
            .join(VpnServer, VpnServer.id == VpnKey.server_id)
            .where(
                VpnKey.subscription_id == sub_id,
                VpnKey.status == VpnKeyStatus.active,
            )
            .order_by(VpnServer.id.asc())
        )).all()
        rows = [
            (key, server) for key, server in rows
            if server.provider != "private-line-primary"
            and _server_protocol(server) == "marzban"
        ]
        if not rows:
            print("FAIL: no active dedicated Marzban key for subscription")
            return 2

        failures = 0
        for server_id in sorted({server.id for _, server in rows}):
            try:
                result = await reconcile_marzban_server(
                    session,
                    server_id,
                    apply=repair,
                    only_subscription_id=sub_id,
                )
                print(
                    f"server={server_id} checked={result.checked_keys} "
                    f"hosts_fixed={result.repaired_hosts} "
                    f"users_fixed={result.repaired_users} "
                    f"urls_fixed={result.repaired_urls} errors={result.errors}"
                )
                failures += result.errors
            except Exception as exc:
                print(f"FAIL: server={server_id} {type(exc).__name__}: {str(exc)[:160]}")
                failures += 1

        if repair and failures == 0:
            await session.commit()
        else:
            await session.rollback()

        if failures:
            print("FAIL: VLESS/Marzban state is not coherent")
            return 2

        print("CONFIG PASS ONLY: API/database/Reality state is coherent.")
        print(f"RUN: bash scripts/probe_vless_e2e.sh {sub_id}")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--subscription", type=int, required=True)
    parser.add_argument("--repair-url", action="store_true")
    args = parser.parse_args()
    if args.subscription <= 0:
        parser.error("Invalid subscription number")
    raise SystemExit(asyncio.run(main(args.subscription, args.repair_url)))
