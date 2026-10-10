"""Read-only VPN fleet audit using the production reconciliation rules.

No credentials, UUIDs, or VLESS links are printed and nothing is modified.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from sqlalchemy import select

from app.database import async_session_maker
from app.enums import VpnServerStatus
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _make_client, _server_protocol
from app.services.vpn_reconcile import reconcile_marzban_server


async def audit() -> int:
    failed = 0
    checked = 0
    async with async_session_maker() as session:
        servers = (await session.execute(
            select(VpnServer).order_by(VpnServer.id.asc())
        )).scalars().all()
        dedicated = [
            s for s in servers
            if s.provider != "private-line-primary"
            and _server_protocol(s) == "marzban"
            and s.status == VpnServerStatus.active
        ]
        if not dedicated:
            print("FAIL: no active dedicated Marzban VPN nodes")
            return 2

        for server in dedicated:
            checked += 1
            if not server.is_selectable:
                print(f"FAIL: node #{server.id} {server.name}: active but not selectable")
                failed += 1
                continue
            try:
                result = await reconcile_marzban_server(
                    session, server.id, apply=False
                )
                if result.errors:
                    print(
                        f"FAIL: node #{server.id} {server.name}: "
                        f"integrity_errors={result.errors}; keys={result.checked_keys}"
                    )
                    failed += 1
                    continue
                client = _make_client(server)
                try:
                    headers = await client._headers()
                    core = await client._client.get(
                        f"{client._base}/api/core", headers=headers
                    )
                    version = core.json().get("version", "?") if core.status_code == 200 else "?"
                    endpoint = urlsplit(server.api_url).hostname or "?"
                    print(
                        f"PASS: node #{server.id} {server.name}; "
                        f"public={endpoint}; Xray={version}; "
                        f"active_keys_checked={result.checked_keys}"
                    )
                finally:
                    await client.aclose()
            except Exception as exc:
                print(
                    f"FAIL: node #{server.id} {server.name}: "
                    f"{type(exc).__name__}: {str(exc)[:150]}"
                )
                failed += 1

        primary = [s for s in servers if s.provider == "private-line-primary"]
        for server in primary:
            if server.is_selectable or server.status == VpnServerStatus.active:
                print(f"FAIL: control-plane VPN record #{server.id} is still selectable/active")
                failed += 1
            else:
                print(f"PASS: control-plane record #{server.id} excluded from VPN fleet")

    if failed:
        print(f"FLEET AUDIT FAIL: {failed} problem(s), checked={checked}")
        return 2
    print(f"FLEET AUDIT PASS: {checked} dedicated node(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(audit()))
