"""Emergency node-wide Xray DB-user resync.

This is intentionally NOT part of normal provisioning. It is used only after:
1) fresh-user zero-restart hot-add passed,
2) synthetic data-plane E2E passed,
3) an already-issued paid credential still failed real E2E.

At that point a single include_db_users() core rebuild is a bounded recovery
for historical DB/live-runtime drift. Credentials and REALITY keys are never
changed.
"""
from __future__ import annotations

import argparse
import asyncio

from app.database import async_session_maker
from app.enums import VpnServerStatus
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _make_client, _server_protocol


async def run(server_id: int, apply: bool) -> int:
    async with async_session_maker() as db:
        server = await db.get(VpnServer, server_id)
        if (
            server is None
            or server.provider == "private-line-primary"
            or _server_protocol(server) != "marzban"
            or server.status == VpnServerStatus.disabled
        ):
            print("STOP: recoverable Marzban node not found")
            return 2

        client = _make_client(server)
        try:
            if not await client.check_connection():
                print("STOP: Marzban authentication failed")
                return 2
            headers = await client._headers()
            core = await client._client.get(
                f"{client._base}/api/core", headers=headers
            )
            if core.status_code != 200:
                print(f"STOP: core status HTTP {core.status_code}")
                return 2
            data = core.json()
            print(
                f"Node {server.id}: core started={data.get('started')} "
                f"version={data.get('version', 'unknown')}"
            )
            if not apply:
                print("DRY RUN: pass --apply only after a real paid-key E2E failure")
                return 0

            await client.restart_core_with_db_users(
                reason=f"emergency paid-key recovery on node {server.id}"
            )
            print(
                f"PASS: node {server.id} Xray rebuilt from Marzban DB users; "
                "credentials unchanged"
            )
            return 0
        except Exception as exc:
            print(f"FAIL: {type(exc).__name__}: {str(exc)[:180]}")
            return 2
        finally:
            await client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.server <= 0:
        parser.error("--server must be positive")
    raise SystemExit(asyncio.run(run(args.server, args.apply)))
