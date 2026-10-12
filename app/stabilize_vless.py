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
from datetime import datetime

from app.enums import SubscriptionStatus, VpnKeyStatus
from app.models.subscription import Subscription
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _make_client, _server_protocol
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
            candidates = (await db.execute(
                select(VpnKey, Subscription)
                .join(Subscription, Subscription.id == VpnKey.subscription_id)
                .where(
                    VpnKey.server_id == server.id,
                    VpnKey.status == VpnKeyStatus.active,
                    Subscription.status.in_([
                        SubscriptionStatus.active,
                        SubscriptionStatus.expires_soon,
                    ]),
                    Subscription.expires_at > datetime.utcnow(),
                )
                .order_by(Subscription.id.desc(), VpnKey.id.desc())
            )).all()
            if not candidates:
                print("FAIL: no active subscription key on this node")
                return 2

            # Do not quarantine a healthy node merely because the newest local
            # row is quota-exhausted or remotely limited. Select a credential
            # that Marzban itself currently considers active; the subsequent
            # Xray E2E probe still proves the real tunnel.
            client = _make_client(server)
            try:
                if not await client.check_connection():
                    print("FAIL: Marzban authentication unavailable")
                    return 2
                headers = await client._headers()
                for key, _subscription in candidates:
                    response = await client._client.get(
                        f"{client._base}/api/user/{key.outline_key_id}",
                        headers=headers,
                    )
                    if response.status_code != 200:
                        continue
                    remote = response.json()
                    if remote.get("status") != "active":
                        continue
                    limit = int(remote.get("data_limit") or 0)
                    used = int(remote.get("used_traffic") or 0)
                    if limit > 0 and used >= limit:
                        continue
                    print(key.subscription_id)
                    return 0
            finally:
                await client.aclose()

            print("FAIL: no remotely active non-exhausted paid key on this node")
            return 2

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
