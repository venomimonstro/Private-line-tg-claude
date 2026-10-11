"""Emit one node's SSH access for the host rollout script.

The secret is base64-encoded only for transport through stdout; its at-rest
storage remains Fernet-encrypted. The host script captures this output and never
prints it.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json

from app.database import async_session_maker
from app.models.vpn_server import VpnServer
from app.services.node_ssh import get_node_ssh_access


async def run(server_id: int) -> int:
    async with async_session_maker() as db:
        server = await db.get(VpnServer, server_id)
        if server is None:
            print(json.dumps({"error": "server_not_found"}))
            return 2
        try:
            access = get_node_ssh_access(server)
        except Exception as exc:
            print(json.dumps({"error": type(exc).__name__, "configured": False}))
            return 3

        print(json.dumps({
            "server_id": server.id,
            "host": access.host,
            "port": access.port,
            "username": access.username,
            "auth_type": access.auth_type,
            "secret_b64": base64.b64encode(access.secret.encode()).decode(),
        }, separators=(",", ":")))
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", type=int, required=True)
    args = parser.parse_args()
    if args.server <= 0:
        parser.error("--server must be positive")
    raise SystemExit(asyncio.run(run(args.server)))
