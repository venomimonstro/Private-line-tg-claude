"""Rank admitted nodes by VPN TCP latency from the control plane."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from app.enums import VpnServerStatus
from app.services.vpn_key import _make_client, _server_protocol


def node_is_ready(server, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    return bool(
        server.status == VpnServerStatus.active
        and server.is_selectable
        and server.provider != "private-line-primary"
        and _server_protocol(server) == "marzban"
        and server.last_e2e_success_at
        and server.last_e2e_success_at >= now - timedelta(minutes=3)
        and server.last_hot_add_success_at
        and server.last_hot_add_success_at >= now - timedelta(minutes=45)
    )


async def vpn_latency(server) -> float | None:
    """Resolve authoritative VPN port; never rank the management API port."""
    client = None
    writer = None
    try:
        async with asyncio.timeout(8):
            host = urlsplit(server.api_url).hostname
            if not host:
                return None
            client = _make_client(server)
            config = await client._get_core_config()
            inbounds = [i for i in (config or {}).get("inbounds", [])
                        if i.get("protocol") == "vless"
                        and i.get("streamSettings", {}).get("security") == "reality"]
            if len(inbounds) != 1:
                return None
            port = inbounds[0].get("port")
            if type(port) is not int or not 1 <= port <= 65535:
                return None
            started = time.monotonic()
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 3)
            return (time.monotonic() - started) * 1000
    except Exception:
        return None
    finally:
        if writer is not None:
            writer.close()
        if client is not None:
            await client.aclose()


async def rank_nodes(servers):
    semaphore = asyncio.Semaphore(6)

    async def measure(server):
        async with semaphore:
            return server, await vpn_latency(server)

    results = await asyncio.gather(*(measure(s) for s in servers if node_is_ready(s)))
    return sorted(results, key=lambda row: (
        row[1] is None, row[1] if row[1] is not None else float("inf"),
        row[0].current_users, row[0].id,
    ))
