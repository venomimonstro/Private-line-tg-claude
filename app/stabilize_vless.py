"""Deprecated compatibility wrapper.

The historical one-node stabilizer had its own repair sequence. Production now
uses one preserve-first fleet reconciler plus the external acceptance gate.
"""
from __future__ import annotations

import argparse
import asyncio
from urllib.parse import urlsplit

from sqlalchemy import select

from app.database import async_session_maker
from app.enums import VpnKeyStatus
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _server_protocol
from app.services.vpn_reconcile import reconcile_marzban_server


async def run(node: str, probe_only: bool = False) -> int:
    async with async_session_maker() as db:
        servers = (await db.execute(
            select(VpnServer).order_by(VpnServer.id.asc())
        )).scalars().all()
        servers = [
            s for s in servers
            if urlsplit(s.api_url).hostname == node
            and s.provider != "private-line-primary"
            and _server_protocol(s) == "marzban"
        ]
        if len(servers) != 1:
            print("FAIL: expected exactly one dedicated Marzban node with this address")
            return 2
        server = servers[0]

        if probe_only:
            sub_id = await db.scalar(
                select(VpnKey.subscription_id)
                .where(
                    VpnKey.server_id == server.id,
                    VpnKey.status == VpnKeyStatus.active,
                )
                .order_by(VpnKey.subscription_id.asc())
                .limit(1)
            )
            if not sub_id:
                print("FAIL: no active subscription key on this node")
                return 2
            print(sub_id)
            return 0

        result = await reconcile_marzban_server(db, server.id, apply=True)
        if result.errors:
            await db.rollback()
            print(f"FAIL: preserve-first reconciliation errors={result.errors}")
            return 2
        await db.commit()
        print(
            f"PASS: server={server.id} keys={result.checked_keys} "
            f"users_fixed={result.repaired_users} urls_fixed={result.repaired_urls} "
            f"hosts_fixed={result.repaired_hosts}"
        )
        print("NEXT: run scripts/production_vpn_gate.sh <SUBSCRIPTION_ID>")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", required=True)
    parser.add_argument("--probe-subscription", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.node, args.probe_subscription)))
