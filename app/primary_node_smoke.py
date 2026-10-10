"""Temporary primary-node VPN account used by a local end-to-end smoke test.

No paid user data or current VLESS credentials are changed. The TEST username
is unique, kept in the invoker's temporary state, and removed on shell exit.
Never log or paste stdout from --create: it contains an actual VPN access URL.
"""
from __future__ import annotations

import argparse
import asyncio
import re
from urllib.parse import urlsplit

from sqlalchemy import select

from app.database import async_session_maker
from app.enums import VpnServerStatus
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _make_client
from app.services.vpn_link import canonical_client_link

PROVIDER = "private-line-primary"
TEST_NAME = re.compile(r"pltest_[0-9a-f]{12}\Z")


async def main(action: str, name: str) -> int:
    if action in {"create", "delete"} and not TEST_NAME.fullmatch(name):
        print("STOP: invalid temporary test username")
        return 2
    async with async_session_maker() as session:
        rows = (await session.execute(
            select(VpnServer).where(VpnServer.provider == PROVIDER)
        )).scalars().all()
        if len(rows) != 1:
            print("STOP: primary node not registered")
            return 2
        server = rows[0]
        if server.api_url != "http://private-line-primary-node:8000":
            print("STOP: wrong primary API URL")
            return 2
        client = _make_client(server)
        try:
            if not await client.check_connection():
                print("STOP: primary Marzban API unavailable")
                return 2
            if action == "create":
                core = await client._client.get(
                    f"{client._base}/api/core", headers=await client._headers()
                )
                if core.status_code != 200 or core.json().get("started") is not True:
                    print("STOP: primary node core stopped")
                    return 2
                vpn = await client.create_key(name=name)
                uri = canonical_client_link(vpn.access_url, "Private Line VPN")
                # Never rely on a hard-coded VPS IP. Compare the actual
                # generated client endpoint with the configured public host.
                hosts = await client._client.get(
                    f"{client._base}/api/hosts",
                    headers=await client._headers()
                )
                host = urlsplit(uri).hostname
                if hosts.status_code != 200 or not any(
                    item.get("address") == host
                    for values in hosts.json().values()
                    if isinstance(values, list)
                    for item in values
                    if isinstance(item, dict)
                ):
                    print("STOP: test key host differs from published Marzban hosts")
                    return 2
                print(uri)
                return 0

            if action == "delete":
                await client.delete_key(name)
                print("TEST USER REMOVED")
                return 0

            if action == "activate":
                core = await client._client.get(
                    f"{client._base}/api/core", headers=await client._headers()
                )
                if core.status_code != 200 or core.json().get("started") is not True:
                    print("STOP: primary node core stopped")
                    return 2
                if not str(core.json().get("version", "")).startswith("26.9.9"):
                    print("STOP: primary Xray version does not match pinned version")
                    return 2
                if server.status != VpnServerStatus.active:
                    print("STOP: primary node is disabled")
                    return 2
                server.is_selectable = True
                await session.commit()
                print("PASS: primary server is now selectable in Telegram")
                return 0

            print("STOP: unknown command")
            return 2
        except Exception as exc:
            print(f"FAIL: {type(exc).__name__}: primary node smoke operation failed")
            return 2
        finally:
            await client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["create", "delete", "activate"])
    parser.add_argument("--name", default="")
    options = parser.parse_args()
    raise SystemExit(asyncio.run(main(options.action, options.name)))
