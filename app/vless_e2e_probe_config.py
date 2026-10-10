"""Create ephemeral Xray config for end-to-end VLESS Reality test.

Reads either a database subscription ID or a private VLESS URL supplied via stdin.
The generated config contains credentials and MUST be written to a mode-0600
temporary file and securely removed immediately after the probe.
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
from app.services.vpn_key import get_access_url


def build_config_from_url(raw: str, socks_port: int) -> dict:
    parsed = urlsplit(raw.strip())
    if parsed.scheme != "vless" or not parsed.hostname or not parsed.username:
        raise ValueError("Not a valid VLESS URL")
    try:
        uid = str(UUID(parsed.username))
        host = str(ipaddress.ip_address(parsed.hostname))
    except ValueError as exc:
        raise ValueError("VLESS UUID or destination IP is malformed") from exc
    if not ipaddress.ip_address(host).is_global:
        raise ValueError("VLESS destination must be a public IP")
    if parsed.port is None or not (1 <= parsed.port <= 65535):
        raise ValueError("Invalid VLESS port")
    args = parse_qs(parsed.query)
    get = lambda name: args.get(name, [""])[0]
    pub, sid, sni = get("pbk"), get("sid"), get("sni")
    if (
        get("security") != "reality"
        or get("flow") != "xtls-rprx-vision"
        or get("encryption") not in ("", "none")
        or get("type") not in ("tcp", "raw")
        or get("fp") != "chrome"
        or get("headerType") not in ("", "none")
    ):
        raise ValueError("VLESS share link differs from client Reality/Vision settings (security/flow/type/fp)")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,44}", pub):
        raise ValueError("Invalid Reality public key")
    if not re.fullmatch(r"(?:[a-f0-9]{2}){1,8}", sid):
        raise ValueError("Invalid Reality short ID")
    if not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", sni):
        raise ValueError("Invalid Reality SNI")
    if len(base64.urlsafe_b64decode(pub + "=" * (-len(pub) % 4))) != 32:
        raise ValueError("Invalid Reality key size")
    return {
        "log": {"loglevel": "info"},
        "inbounds": [{
            "tag": "probe-socks", "listen": "127.0.0.1", "port": socks_port,
            "protocol": "socks", "settings": {"auth": "noauth", "udp": False},
        }],
        "outbounds": [{
            "tag": "via-node", "protocol": "vless",
            "settings": {"vnext": [{
                "address": host, "port": parsed.port,
                "users": [{"id": uid, "encryption": "none", "flow": "xtls-rprx-vision"}],
            }]},
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


async def get_url_from_db(subscription_id: int) -> str:
    async with async_session_maker() as db:
        sub = await db.get(Subscription, subscription_id)
        if sub is None:
            # Include non-confidential IDs only, to explain why previous checks
            # succeeded but the current probe has no matching subscription.
            rows = (await db.execute(
                select(Subscription.id, Subscription.status)
                .order_by(Subscription.id.desc()).limit(10)
            )).all()
            summary = ", ".join(f"{id}:{status.value}" for id, status in rows) or "(none)"
            raise ValueError(
                f"Subscription {subscription_id} not found in the current database. "
                f"Existing subscriptions: {summary}"
            )
        keys = (await db.execute(select(VpnKey).where(
            VpnKey.subscription_id == subscription_id,
            VpnKey.status == VpnKeyStatus.active,
        ))).scalars().all()
        if len(keys) != 1:
            raise ValueError(f"Subscription {subscription_id} has {len(keys)} active keys, expected 1")
        return get_access_url(keys[0])


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--subscription", type=int)
    mode.add_argument("--stdin-url", action="store_true",
                      help="Read full VLESS URL from stdin (never supplied as a command-line argument)")
    parser.add_argument("--socks-port", default=19580, type=int)
    options = parser.parse_args()
    if (options.subscription is not None and options.subscription <= 0) or not (1025 <= options.socks_port <= 65535):
        parser.error("Invalid subscription number or SOCKS port")
    try:
        if options.stdin_url:
            raw = sys.stdin.readline(8192).strip()
            if not raw:
                raise ValueError("No VLESS URL supplied via stdin")
        else:
            raw = asyncio.run(get_url_from_db(options.subscription))
        config = build_config_from_url(raw, options.socks_port)
    except Exception as exc:
        print(f"Cannot prepare client probe: {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        raise SystemExit(2)
    json.dump(config, sys.stdout, ensure_ascii=True, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
