"""Canonical user-facing VPN share links.

Keep the original encrypted link untouched in storage. Only the display and
import representation is normalised, so users receive a well-formed VLESS
REALITY URI. Reject unsupported/missing client settings rather than silently
relying on Xray probe defaults.
"""
from __future__ import annotations

from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit
from uuid import UUID
import re

_PBK = re.compile(r"[A-Za-z0-9_-]{43,44}\Z")
_HEX = re.compile(r"(?:[0-9a-fA-F]{2}){1,8}\Z")
_SNI = re.compile(r"[A-Za-z0-9.-]{1,253}\Z")


def canonical_client_link(raw_url: str, label: str = "Private Line VPN") -> str:
    """Convert a VLESS Reality URL into a standard single-fragment share URI.

    Non-VLESS links are retained for providers that use their own formats.
    No credentials are generated, stored, rotated or logged.
    """
    raw_url = raw_url.strip()
    parsed = urlsplit(raw_url)
    if parsed.scheme != "vless":
        return raw_url
    try:
        user_id = str(UUID(parsed.username or ""))
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid VLESS UUID/port") from exc
    if not host or not port or not 1 <= port <= 65535:
        raise ValueError("VLESS host or port is invalid")
    query = parse_qs(parsed.query, keep_blank_values=True)

    def param(key: str, default: str = "") -> str:
        values = query.get(key)
        if values and len(values) != 1:
            raise ValueError(f"Ambiguous VLESS parameter: {key}")
        return values[0] if values else default

    if (
        param("security") != "reality"
        or param("flow") != "xtls-rprx-vision"
        or param("type", "tcp") not in ("tcp", "raw")
        or param("encryption", "none") != "none"
        or param("headerType", "none") not in ("", "none")
    ):
        raise ValueError("Unsupported VLESS REALITY/Vision transport")
    public_key = param("pbk")
    sid = param("sid")
    sni = param("sni")
    fingerprint = param("fp", "chrome")
    if not _PBK.fullmatch(public_key):
        raise ValueError("Missing or invalid Reality public key")
    if not _HEX.fullmatch(sid):
        raise ValueError("Invalid Reality short ID")
    if not _SNI.fullmatch(sni):
        raise ValueError("Missing or invalid Reality SNI")
    if fingerprint != "chrome":
        raise ValueError("Unsupported fingerprint for this VLESS profile")

    # Preserve the endpoint and public cryptographic settings. The original
    # fragment is a display name only and never contains protocol settings.
    hostname = f"[{host}]" if ":" in host and not host.startswith("[") else host
    settings = {
        "encryption": "none",
        "security": "reality",
        "sni": sni,
        "fp": fingerprint,
        "pbk": public_key,
        "sid": sid,
        "type": "tcp",
        "headerType": "none",
        "flow": "xtls-rprx-vision",
    }
    fragment = quote(label.strip()[:64] or "Private Line VPN", safe="-._~")
    return urlunsplit(("vless", f"{user_id}@{hostname}:{port}", "", urlencode(settings), fragment))
