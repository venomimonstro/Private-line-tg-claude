"""Safely resync a Marzban Xray core with active users stored in Marzban DB.

Run on the MAIN server:
  docker compose exec -T admin python -m app.resync_marzban_core --subscription 3
  docker compose exec -T admin python -m app.resync_marzban_core --subscription 3 --apply

This invokes Marzban's POST /api/core/restart, which builds its runtime config
with include_db_users(). It does not modify keys, UUIDs, Marzban databases or
Reality private keys. It may briefly interrupt connections to this node.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select

from app.database import async_session_maker
from app.enums import SubscriptionStatus, VpnKeyStatus
from app.models.subscription import Subscription
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _make_client, _server_protocol, get_access_url

ELIGIBLE = {SubscriptionStatus.active, SubscriptionStatus.expires_soon}


async def resync(sub_id: int, apply: bool) -> int:
    async with async_session_maker() as db:
        sub = await db.get(Subscription, sub_id)
        if not sub or sub.status not in ELIGIBLE:
            print("STOP: no active subscription")
            return 2
        if not sub.expires_at or sub.expires_at <= datetime.utcnow():
            print("STOP: subscription has expired")
            return 2
        keys = (await db.execute(
            select(VpnKey).where(
                VpnKey.subscription_id == sub_id,
                VpnKey.status == VpnKeyStatus.active,
            )
        )).scalars().all()
        if len(keys) != 1 or keys[0].user_id != sub.user_id:
            print("STOP: cannot identify exactly one valid active VPN key")
            return 2
        key = keys[0]
        server = await db.get(VpnServer, key.server_id)
        if not server or _server_protocol(server) != "marzban":
            print("STOP: subscription has no Marzban server")
            return 2
        try:
            link = urlsplit(get_access_url(key))
            original_uuid = str(UUID(link.username or ""))
            if link.scheme != "vless" or link.hostname != urlsplit(server.api_url).hostname:
                raise ValueError("VLESS key mismatch")
        except (ValueError, TypeError):
            print("STOP: invalid stored VLESS key")
            return 2
        client = _make_client(server)
        try:
            if not await client.check_connection():
                print("STOP: Marzban authentication failed")
                return 2
            headers = await client._headers()
            resp = await client._client.get(
                f"{client._base}/api/user/{key.outline_key_id}",
                headers=headers,
            )
            if resp.status_code != 200:
                print(f"STOP: Marzban user unavailable (HTTP {resp.status_code})")
                return 2
            user = resp.json()
            user_uuid = user.get("proxies", {}).get("vless", {}).get("id", "")
            status = user.get("status")
            inbounds = user.get("inbounds", {}).get("vless") or []
            flow = user.get("proxies", {}).get("vless", {}).get("flow")
            if (
                user_uuid != original_uuid
                or status != "active"
                or flow != "xtls-rprx-vision"
                or not inbounds
            ):
                print("STOP: Marzban user identity/status/flow/inbounds mismatch")
                return 2
            config = await client._get_core_config()
            allowed = {
                i.get("tag")
                for i in (config or {}).get("inbounds", [])
                if i.get("protocol") == "vless"
                and i.get("streamSettings", {}).get("security") == "reality"
            }
            if not allowed.intersection(inbounds):
                print("STOP: user not assigned to a Reality inbound")
                return 2

            stats = await client._client.get(f"{client._base}/api/core", headers=headers)
            if stats.status_code != 200:
                print(f"STOP: core status API returned HTTP {stats.status_code}")
                return 2
            core = stats.json()
            print(
                f"Marzban user: active, UUID verified; "
                f"Xray version: {core.get('version', 'unknown')}; "
                f"started={core.get('started')}"
            )
            if not apply:
                print("DRY RUN: use --apply for a controlled Marzban Xray core resync")
                return 0

            print("Resyncing Xray with users from the Marzban database...")
            result = await client._client.post(
                f"{client._base}/api/core/restart",
                headers=headers,
                timeout=60.0,
            )
            if result.status_code not in (200, 204):
                print(f"FAIL: Marzban core restart returned HTTP {result.status_code}")
                return 2

            for _ in range(30):
                await asyncio.sleep(2)
                try:
                    s = await client._client.get(
                        f"{client._base}/api/core", headers=headers, timeout=4.0
                    )
                    if s.status_code == 200 and s.json().get("started") is True:
                        print("PASS: Marzban reports Xray restarted with DB users")
                        print("NEXT: run 'bash scripts/probe_vless_e2e.sh 3' on main server")
                        return 0
                except Exception:
                    continue
            print("FAIL: Xray did not report a started core; inspect node logs")
            return 2
        except Exception as exc:
            print(f"FAIL: {type(exc).__name__}: {str(exc)[:160]}")
            return 2
        finally:
            await client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--subscription", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.subscription <= 0:
        parser.error("Invalid subscription ID")
    raise SystemExit(asyncio.run(resync(args.subscription, args.apply)))
