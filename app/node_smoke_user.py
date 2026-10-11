"""Disposable Marzban user helper for end-to-end node smoke tests.

Stdout from create contains a temporary VLESS credential and must only be
redirected to a mode-0600 temp file. No paid user credentials are touched.
"""
from __future__ import annotations

import argparse
import asyncio
import re

from app.database import async_session_maker
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _make_client, _server_protocol
from app.services.vpn_link import canonical_client_link

_TEST_NAME = re.compile(r"pltest_[0-9a-f]{16}\Z")


async def run(action: str, server_id: int, name: str) -> int:
    if server_id <= 0 or not _TEST_NAME.fullmatch(name):
        print("STOP: invalid smoke-test arguments")
        return 2
    async with async_session_maker() as session:
        server = await session.get(VpnServer, server_id)
        if server is None or _server_protocol(server) != "marzban":
            print("STOP: Marzban node not found")
            return 2
        if server.provider == "private-line-primary":
            print("STOP: control-plane VPS is excluded from VPN tests")
            return 2
        client = _make_client(server)
        try:
            if not await client.check_connection():
                print("STOP: Marzban authentication failed")
                return 2
            if action == "create":
                key = await client.create_key(name=name)
                print(canonical_client_link(key.access_url, "Private Line VPN"))
                return 0
            if action == "delete":
                await client.delete_key(name)
                if not await client.user_absent(name):
                    print("FAIL: temporary user still exists after cleanup verification")
                    return 2
                print("PASS: disposable test user removed")
                return 0
            print("STOP: unsupported action")
            return 2
        except Exception as exc:
            print(f"FAIL: {type(exc).__name__}: smoke user operation failed")
            return 2
        finally:
            await client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["create", "delete"])
    parser.add_argument("--server", required=True, type=int)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.action, args.server, args.name)))
