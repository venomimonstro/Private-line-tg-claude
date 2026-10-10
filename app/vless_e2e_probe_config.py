"""Export a temporary Xray REALITY client config for an end-to-end probe.

Used exclusively by scripts/probe_vless_e2e.sh. Prints confidential Xray JSON to
stdout; redirect to a mode-0600 temporary file. Never print the output in chat.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import ipaddress
import json
import re
import sys
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from sqlalchemy import select

from app.database import async_session_maker
from app.enums import VpnKeyStatus
from app.models.subscription import Subscription
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.vpn_key import get_access_url, _server_protocol


async def get_config(subscription_id: int, socks_port: int) -> dict:
    async with async_session_maker() as db:
        sub = await db.get(Subscription, subscription_id)
        if sub is None:
            raise ValueError("Subscription not found")

        rows = (await db.execute(select(VpnKey).where(
            VpnKey.subscription_id == subscription_id,
            VpnKey.status == VpnKeyStatus.active,
        ))).scalars().all()
        if len(rows) != 1:
            raise ValueError(f"Expected exactly one active VPN key, found {len(rows)}")
        key = rows[0]
        server = await db.get(VpnServer, key.server_id)
        if server is None or _server_protocol(server) != "marzban":
            raise ValueError("This probe supports Marzban VLESS nodes only")

        raw = get_access_url(key)
        parsed = urlsplit(raw)
        if parsed.scheme != "vless" or not parsed.hostname or not parsed.username:
            raise ValueError("Stored VLESS key is malformed")
        try:
            uid = str(UUID(parsed.username))
        except ValueError as exc:
            raise ValueError("Invalid UUID in key") from exc

        host = str(ipaddress.ip_address(parsed.hostname))
        if parsed.port is None or not (1 <= parsed.port <= 65535):
            raise ValueError("Invalid VPN port")
        args = parse_qs(parsed.query)
        get = lambda name: args.get(name, [""])[0]
        pub, sid, sni = get("pbk"), get("sid"), get("sni")
        if (
            get("security") != "reality"
            or get("flow") != "xtls-rprx-vision"
            or get("encryption") not in ("", "none")
        ):
            raise ValueError("VLESS key does not match Reality Vision settings")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43,44}", pub):
            raise ValueError("Invalid Reality public key")
        if not re.fullmatch(r"(?:[a-f0-9]{2}){1,8}", sid):
            raise ValueError("Invalid Reality short ID")
        if not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", sni):
            raise ValueError("Invalid SNI")
        try:
            if len(base64.urlsafe_b64decode(pub + "=" * (-len(pub) % 4))) != 32:
                raise ValueError("Wrong public key size")
        except Exception as exc:
            raise ValueError("Invalid Reality public key bytes") from exc

        return {
            "log": {"loglevel": "warning"},
            "inbounds": [{
                "tag": "probe-socks",
                "listen": "127.0.0.1",
                "port": socks_port,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": False},
            }],
            "outbounds": [{
                "tag": "via-node",
                "protocol": "vless",
                "settings": {
                    "vnext": [{
                        "address": host,
                        "port": parsed.port,
                        "users": [{
                            "id": uid,
                            "encryption": "none",
                            "flow": "xtls-rprx-vision",
                        }],
                    }],
                },
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "serverName": sni,
                        "fingerprint": "chrome",
                        "password": pub,
                        "shortId": sid,
                        "spiderX": "",
                    },
                },
            }],
            "routing": {"domainStrategy": "AsIs", "rules": []},
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subscription", required=True, type=int)
    parser.add_argument("--socks-port", default=19580, type=int)
    options = parser.parse_args()
    if options.subscription <= 0 or not (1025 <= options.socks_port <= 65535):
        parser.error("Invalid subscription ID or SOCKS port")
    try:
        config = asyncio.run(get_config(options.subscription, options.socks_port))
    except Exception as exc:
        print(f"Cannot prepare client probe: {type(exc).__name__}: {str(exc)[:160]}", file=sys.stderr)
        raise SystemExit(2)
    json.dump(config, sys.stdout, ensure_ascii=True, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
