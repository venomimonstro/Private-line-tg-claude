"""Register the isolated co-located Marzban primary VPN node.

The setup shell script writes a private JSON object to stdin:
  {"username": "...", "password": "...", "public_ip": "..."}
No credentials or VLESS URLs are printed. It is deliberately separate from the
Spanish server and does not edit the main app's Docker Compose or database
structure.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import sys

from sqlalchemy import select

from app.database import async_session_maker
from app.encryption import encrypt
from app.enums import VpnServerStatus
from app.models.vpn_server import VpnServer
from app.vpn.marzban import MarzbanClient

API = "http://private-line-primary-node:8000"
PROVIDER = "private-line-primary"
NAME = "Основной сервер (временно)"


async def apply(data: dict, commit: bool) -> int:
    try:
        ip = str(ipaddress.IPv4Address(data["public_ip"]))
        if not ipaddress.IPv4Address(ip).is_global:
            raise ValueError("Non-public IP")
        public_port = int(data.get("public_port", 443))
        if public_port not in (443, 8443):
            raise ValueError("invalid primary port")
        user, password = data["username"], data["password"]
        if not isinstance(user, str) or not user or not isinstance(password, str) or len(password) < 12:
            raise ValueError("Invalid Marzban credentials")
    except (ValueError, TypeError, KeyError):
        print("STOP: invalid primary node IP/credentials")
        return 2

    client = MarzbanClient(API, user, password)
    try:
        if not await client.check_connection():
            print("STOP: primary node API authentication failed")
            return 2
        cfg = await client._get_core_config()
        reality = await client.get_reality_params()
        if not cfg or not reality:
            print("STOP: primary node Reality configuration unavailable")
            return 2
        inbounds = [inbound for inbound in cfg.get("inbounds", [])
            if inbound.get("protocol") == "vless"
            and inbound.get("streamSettings", {}).get("security") == "reality"]
        if len(inbounds) != 1 or inbounds[0].get("port") != 443:
            print("STOP: expected exactly one VLESS Reality inbound on port 443")
            return 2
        tag = inbounds[0].get("tag")
        _, _, sni = reality
        headers = await client._headers()
        core = await client._client.get(f"{API}/api/core", headers=headers)
        if core.status_code != 200 or core.json().get("started") is not True:
            print("STOP: Marzban reports Xray as stopped")
            return 2
        version = str(core.json().get("version", ""))
        if not version.startswith("26.9.9"):
            print(f"STOP: expected pinned Xray 26.9.9; got {version[:25]}")
            return 2

        response = await client._client.get(f"{API}/api/hosts", headers=headers)
        if response.status_code != 200 or not isinstance(response.json(), dict):
            print("STOP: primary Marzban /api/hosts not available")
            return 2
        hosts = response.json()
        expected = {
            "remark": NAME,
            "address": ip,
            "port": public_port,
            "sni": sni,
            "security": "inbound_default",
            "fingerprint": "chrome",
            "is_disabled": False,
        }
        entries = hosts.get(tag)
        if entries is not None and (not isinstance(entries, list) or len(entries) > 1):
            print("STOP: existing Marzban hosts ambiguous; no changes")
            return 2
        if not commit:
            print(f"DRY RUN: primary Xray {version}, inbound={tag}, IPv4={ip}")
            print("DRY RUN: no changes made; pass --apply to publish this VPN node")
            return 0

        # This is a NEW Marzban instance, but preserve any existing entry's
        # extra keys to avoid discarding optional provider-specific settings.
        base = entries[0] if entries else {}
        hosts[tag] = [{**base, **expected}]
        put = await client._client.put(
            f"{API}/api/hosts",
            headers={**headers, "Content-Type": "application/json"},
            json=hosts,
        )
        if put.status_code != 200:
            print(f"STOP: failed to configure public VPN host (HTTP {put.status_code})")
            return 2
        check = await client._client.get(f"{API}/api/hosts", headers=headers)
        if check.status_code != 200:
            print("STOP: could not verify VPN host configuration")
            return 2
        saved = check.json().get(tag, [])
        if not saved or saved[0].get("address") != ip or saved[0].get("security") != "inbound_default":
            print("STOP: public host settings did not persist")
            return 2

        async with async_session_maker() as db:
            existing = (await db.execute(select(VpnServer).where(
                VpnServer.provider == PROVIDER,
            ))).scalars().all()
            if len(existing) > 1:
                print("STOP: multiple primary node records; manual reconciliation")
                return 2
            if existing:
                row = existing[0]
                if row.api_url != API:
                    print("STOP: existing primary entry has another API address")
                    return 2
                row.status = VpnServerStatus.active
                # Keep its previous publish state on repeated configuration runs.
                # The primary node must pass an actual VLESS smoke test first.
            else:
                row = VpnServer(
                    name=NAME,
                    country="Основной",
                    city="Основной сервер",
                    country_code=None,
                    flag_emoji="🏠",
                    provider=PROVIDER,
                    api_url=API,
                    api_secret_encrypted="",
                    status=VpnServerStatus.active,
                    is_selectable=False,
                    max_users=20,
                    current_users=0,
                )
                db.add(row)
            row.api_secret_encrypted = encrypt(json.dumps(
                {"protocol": "marzban", "username": user, "password": password},
            ))
            row.name = NAME
            row.country = "Основной"
            row.flag_emoji = "🏠"
            await db.commit()
            print(f"REGISTERED: verified main VPN node, server ID={row.id}, public IPv4={ip}")
            print("STAGED: run the VLESS smoke test before publishing this server in Telegram")
            return 0
    except Exception as exc:
        print(f"FAIL: primary node setup ({type(exc).__name__}); check container logs locally")
        return 2
    finally:
        await client.aclose()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    try:
        payload = json.loads(sys.stdin.readline(8192))
        if not isinstance(payload, dict):
            raise ValueError("Invalid input")
    except (ValueError, TypeError):
        print("STOP: expected JSON credentials on standard input")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(apply(payload, a.apply)))
