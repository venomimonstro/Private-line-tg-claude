"""Diagnose a stored VLESS Reality key against the live Marzban node.

Run in main server's admin container:
  docker compose exec -T admin python -m app.diagnose_vless --subscription 2
  docker compose exec -T admin python -m app.diagnose_vless --subscription 2 --repair-url

--repair-url updates only the stored encrypted VLESS URL if it differs from the
verified API/config values, preserving UUID, Marzban user, and Reality private key.
Never prints access URLs or credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import sys
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from sqlalchemy import select

from app.database import async_session_maker
from app.enums import VpnKeyStatus
from app.models.subscription import Subscription
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _make_client, _server_protocol, get_access_url
from app.encryption import encrypt


async def port_open(host: str, port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def main(sub_id: int, repair: bool) -> int:
    async with async_session_maker() as session:
        subscription = await session.get(Subscription, sub_id)
        if subscription is None:
            print("FAIL: subscription not found")
            return 2

        keys = (await session.execute(
            select(VpnKey).where(
                VpnKey.subscription_id == sub_id,
                VpnKey.status == VpnKeyStatus.active,
            )
        )).scalars().all()
        if len(keys) != 1:
            print(f"FAIL: expected exactly one active key, found {len(keys)}")
            return 2
        key = keys[0]
        server = await session.get(VpnServer, key.server_id)
        if server is None or _server_protocol(server) != "marzban":
            print("FAIL: server missing or is not Marzban")
            return 2

        try:
            public_host = urlsplit(server.api_url).hostname
            if public_host is None or not ipaddress.ip_address(public_host).is_global:
                raise ValueError("Node API must contain a public routable address")
            parsed = urlsplit(get_access_url(key))
            if parsed.scheme != "vless" or not parsed.username:
                raise ValueError("Stored key has invalid VLESS URI")
            client_host = parsed.hostname
            client_port = parsed.port
            uuid = parsed.username
        except (ValueError, TypeError) as exc:
            print(f"FAIL: invalid server/client URL: {exc}")
            return 2

        print(f"Node: {server.name}; API host: {public_host}")
        print(f"Key destination: {client_host}:{client_port}; subscription={sub_id}")
        tcp_ok = await port_open(public_host, 443)
        print(f"Main server -> node TCP/443: {'OPEN' if tcp_ok else 'CLOSED/TIMEOUT'}")

        client = _make_client(server)
        try:
            if not await client.check_connection():
                print("FAIL: Marzban authentication failed")
                return 2

            config = await client._get_core_config()
            reality = await client.get_reality_params()
            if not isinstance(config, dict) or reality is None:
                print("FAIL: Marzban did not return active Xray Reality config")
                return 2
            public_key, short_id, sni = reality
            inbounds = [v for v in config.get("inbounds", [])
                        if v.get("protocol") == "vless"
                        and v.get("streamSettings", {}).get("security") == "reality"]
            if not inbounds:
                print("FAIL: no VLESS Reality inbound")
                return 2
            inbound = inbounds[0]
            inbound_tag = inbound.get("tag")
            port = inbound.get("port")
            if not isinstance(port, int) or not (1 <= port <= 65535):
                print("FAIL: invalid VLESS inbound port")
                return 2
            if not inbound_tag:
                print("FAIL: missing VLESS inbound tag")
                return 2
            print(f"Xray: VLESS Reality inbound={inbound_tag} port={port}")

            response = await client._client.get(
                f"{client._base}/api/user/{key.outline_key_id}",
                headers=await client._headers(),
            )
            if response.status_code != 200:
                print(f"FAIL: stored Marzban user unavailable (HTTP {response.status_code})")
                return 2
            user = response.json()
            user_uuid = user.get("proxies", {}).get("vless", {}).get("id", "")
            user_flow = user.get("proxies", {}).get("vless", {}).get("flow", "")
            user_inbounds = user.get("inbounds", {}).get("vless", [])
            status = user.get("status", "?")
            print(f"Marzban user: status={status}; inbound assigned={inbound_tag in user_inbounds}; flow={user_flow or '(missing)'}")
            if not user_uuid or uuid.lower() != str(user_uuid).lower():
                print("FAIL: VLESS UUID differs from the Marzban user; no automatic changes")
                return 2
            if status != "active" or inbound_tag not in user_inbounds:
                print("FAIL: Marzban user inactive or not assigned to the Reality inbound")
                return 2
            if user_flow != "xtls-rprx-vision":
                print("WARNING: Marzban user flow differs from expected xtls-rprx-vision")
                # Don't rewrite link: server must be repaired independently.
                return 2

            host_resp = await client._client.get(
                f"{client._base}/api/hosts", headers=await client._headers()
            )
            hosts_ok = False
            if host_resp.status_code == 200:
                host_map = host_resp.json()
                matching = host_map.get(inbound_tag, [])
                hosts_ok = any(
                    isinstance(h, dict)
                    and h.get("address") == public_host
                    and h.get("port") == port
                    and not h.get("is_disabled", False)
                    for h in matching
                )
            print(f"Marzban hosts match node address/port: {'OK' if hosts_ok else 'MISMATCH'}")

            opts = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
            problems = []
            if client_host != public_host:
                problems.append("wrong destination IP")
            if client_port != port:
                problems.append("wrong VPN port")
            for key_name, expected in {
                "security": "reality",
                "pbk": public_key,
                "sid": short_id,
                "sni": sni,
                "fp": "chrome",
                "flow": "xtls-rprx-vision",
                "type": "tcp",
            }.items():
                if opts.get(key_name) != expected:
                    problems.append(f"wrong or missing {key_name}")

            if problems:
                print("Key mismatches: " + ", ".join(problems))
            else:
                print("Key parameters match Marzban Reality: OK")

            if not hosts_ok:
                print("ACTION: repair Marzban hosts before using a generated client link")
                return 1
            if not tcp_ok:
                print("ACTION: check Xray listening on 443 and provider firewall; do not rotate keys")
                return 1

            if problems and repair:
                opts.update({
                    "security": "reality",
                    "encryption": "none",
                    "pbk": public_key,
                    "sid": short_id,
                    "sni": sni,
                    "fp": "chrome",
                    "flow": "xtls-rprx-vision",
                    "type": "tcp",
                })
                updated = urlunsplit((
                    "vless",
                    f"{uuid}@{public_host}:{port}",
                    parsed.path,
                    urlencode(opts),
                    parsed.fragment,
                ))
                key.access_url_encrypted = encrypt(updated)
                await session.commit()
                print("REPAIRED: encrypted VLESS URL updated; UUID and Reality keys preserved")
                print("ACTION: use 'My key' in Telegram to obtain the updated link")
                return 0

            if problems:
                print("ACTION: rerun with --repair-url to correct only the saved link")
                return 1
            print("PASS: key and node API agree. If the client still fails, check node Xray logs and client error.")
            return 0
        except Exception as exc:
            # Print exception class, not response objects, tokens, or secrets.
            print(f"FAIL: diagnostic {type(exc).__name__}: {str(exc)[:160]}")
            return 2
        finally:
            await client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--subscription", type=int, required=True)
    parser.add_argument("--repair-url", action="store_true")
    args = parser.parse_args()
    if args.subscription <= 0:
        parser.error("Invalid subscription number")
    sys.exit(asyncio.run(main(args.subscription, args.repair_url)))
