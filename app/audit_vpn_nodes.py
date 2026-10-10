"""Read-only VPN fleet audit (no credentials/UUIDs/shared VLESS links printed).

Run on the main server:
  docker compose exec -T admin python -m app.audit_vpn_nodes
"""
from __future__ import annotations

import asyncio
import ipaddress

from sqlalchemy import select

from app.database import async_session_maker
from app.enums import VpnServerStatus
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _make_client, _server_protocol


async def audit() -> int:
    failed = 0
    async with async_session_maker() as db:
        servers = (await db.execute(
            select(VpnServer).order_by(VpnServer.id)
        )).scalars().all()
        if not servers:
            print("FAIL: no VPN nodes registered")
            return 2
        for server in servers:
            if server.status != VpnServerStatus.active or not server.is_selectable:
                print(f"SKIP: {server.name} (inactive or not selectable)")
                continue
            if _server_protocol(server) != "marzban":
                print(f"INFO: {server.name} (non-Marzban, API probe skipped)")
                continue
            client = _make_client(server)
            try:
                if not await client.check_connection():
                    raise RuntimeError("Marzban API authentication unavailable")
                cfg = await client._get_core_config()
                reality = await client.get_reality_params()
                if not cfg or not reality:
                    raise RuntimeError("Reality not configured")
                inbounds = [i for i in cfg.get("inbounds", [])
                    if i.get("protocol") == "vless"
                    and i.get("streamSettings", {}).get("security") == "reality"]
                if len(inbounds) != 1:
                    raise RuntimeError("Not exactly one Reality inbound")
                tag = inbounds[0].get("tag")
                port = inbounds[0].get("port")
                resp = await client._client.get(
                    f"{client._base}/api/hosts", headers=await client._headers()
                )
                if resp.status_code != 200:
                    raise RuntimeError("Hosts API returned non-200")
                hosts = resp.json().get(tag, [])
                if not isinstance(hosts, list) or not hosts:
                    raise RuntimeError("No published host entries")
                public = [h for h in hosts if not h.get("is_disabled")
                    and isinstance(h.get("address"), str)]
                public = [h for h in public
                    if _public_ip(h.get("address"))]
                if not public:
                    raise RuntimeError("No globally reachable VPN IP in host settings")
                stats = await client._client.get(
                    f"{client._base}/api/core", headers=await client._headers()
                )
                if stats.status_code != 200 or stats.json().get("started") is not True:
                    raise RuntimeError("Xray core is not running")
                print(f"PASS: {server.name}; public={public[0]['address']}:{port}; "
                      f"Xray={stats.json().get('version','?')}; configured capacity={server.max_users}")
            except Exception as exc:
                print(f"FAIL: {server.name}: {type(exc).__name__}: {str(exc)[:130]}")
                failed += 1
            finally:
                await client.aclose()
    return 2 if failed else 0


def _public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(asyncio.run(audit()))
