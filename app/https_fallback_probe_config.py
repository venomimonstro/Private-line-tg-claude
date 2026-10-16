"""Confidential disposable client for a loopback E2E of the public HTTPS relay."""
from __future__ import annotations

import argparse
import ipaddress
import json
from urllib.parse import urlsplit

from app.config import settings
from app.vpn.https_fallback import RELAY_DIR, RELAY_PATH


def config_for_relay(user_id: str, public_url: str, socks_port: int) -> dict:
    public = urlsplit(public_url)
    host = str(ipaddress.ip_address(public.hostname or ""))
    if public.scheme != "https" or public.port not in (None, 443):
        raise ValueError("Trusted HTTPS/443 is required")
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{"listen": "127.0.0.1", "port": socks_port,
                      "protocol": "socks", "settings": {"auth": "noauth", "udp": False}}],
        "outbounds": [{"protocol": "vless", "settings": {"vnext": [
            {"address": "127.0.0.1", "port": 443,
             "users": [{"id": user_id, "encryption": "none"}]}]},
            "streamSettings": {"network": "ws", "security": "tls",
                               "tlsSettings": {"serverName": host},
                               "wsSettings": {"path": RELAY_PATH,
                                              "headers": {"Host": host}}}}],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socks-port", type=int, default=19583)
    parser.add_argument("--node-ip", action="store_true")
    args = parser.parse_args()
    if not 1025 <= args.socks_port <= 65535:
        parser.error("Invalid SOCKS port")
    cfg = json.loads((RELAY_DIR / "config.json").read_text())
    clients = cfg["inbounds"][0]["settings"]["clients"]
    if not clients:
        parser.error("No paid clients in relay")
    if args.node_ip:
        print(cfg["outbounds"][0]["settings"]["vnext"][0]["address"])
    else:
        print(json.dumps(config_for_relay(clients[0]["id"],
                                          settings.CONTROL_PLANE_PUBLIC_URL,
                                          args.socks_port), separators=(",", ":")))


if __name__ == "__main__":
    main()
