"""VPN backends whose VLESS Reality profiles are usable by subscribers."""
from __future__ import annotations

from app.models.vpn_server import VpnServer
from app.services.vpn_key import _server_protocol


def is_vless_node(server: VpnServer) -> bool:
    return _server_protocol(server) in ("direct_xray", "marzban")
