"""A second client ingress via trusted control-plane HTTPS and a local Xray relay."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID

RELAY_DIR = Path("/app/data/vpn_relay")
RELAY_PATH = "/vpn-tunnel"


def relay_uuid(secret: str, server_id: int) -> str:
    """Stable dedicated node credential, never shared with a subscriber."""
    digest = bytearray(hmac.new(secret.encode(), f"vpn-relay:{server_id}".encode(),
                                hashlib.sha256).digest()[:16])
    digest[6] = (digest[6] & 0x0f) | 0x40
    digest[8] = (digest[8] & 0x3f) | 0x80
    return str(UUID(bytes=bytes(digest)))


def fallback_link(primary: str, public_url: str, label: str = "Private Line HTTPS") -> str:
    """VLESS over ordinary TLS/WebSocket to the IP with a public CA certificate."""
    source = urlsplit(primary)
    public = urlsplit(public_url)
    uid = str(UUID(source.username or ""))
    host = str(ipaddress.ip_address(public.hostname or ""))
    if source.scheme != "vless" or public.scheme != "https" or public.port not in (None, 443):
        raise ValueError("HTTPS fallback requires VLESS and public HTTPS/443")
    if not ipaddress.ip_address(host).is_global:
        raise ValueError("HTTPS fallback requires a public control-plane IP")
    # Include every interoperability field explicitly. Some mobile clients do
    # not infer SNI/ALPN correctly when the endpoint is an IP certificate.
    query = urlencode({"encryption": "none", "security": "tls", "sni": host,
                       "fp": "chrome", "alpn": "http/1.1", "type": "ws",
                       "host": host, "path": RELAY_PATH})
    return f"vless://{uid}@{host}:443?{query}#{quote(label[:64], safe='-._~')}"


def published_fallback(primary: str, public_url: str) -> str | None:
    """Only advertise credentials present in a relay that passed its real tunnel gate."""
    if not (RELAY_DIR / "healthy").is_file():
        return None
    try:
        uid = str(UUID(urlsplit(primary).username or ""))
        config = json.loads((RELAY_DIR / "config.json").read_text())
        users = config["inbounds"][0]["settings"]["clients"]
        if not any(row["id"] == uid for row in users):
            return None
        return fallback_link(primary, public_url)
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def preferred_client_link(primary: str, public_url: str) -> str:
    """Return the gated HTTPS profile, falling back to the direct profile."""
    return published_fallback(primary, public_url) or primary
