"""Atomically reconcile the main HTTPS relay with paid users and a dedicated node."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.database import async_session_maker
from app.encryption import decrypt
from app.enums import SubscriptionStatus, VpnKeyStatus, VpnServerStatus
from app.models.subscription import Subscription
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _server_protocol
from app.vpn.https_fallback import RELAY_DIR, RELAY_PATH, relay_uuid


def render(users: list[tuple[str, str]], node: dict, server_id: int) -> dict:
    if node.get("protocol") != "direct_xray":
        raise ValueError("Relay target is not direct Xray")
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{"tag": "public-ws", "listen": "0.0.0.0", "port": 10080,
                      "protocol": "vless", "settings": {"decryption": "none",
                      "clients": [{"id": uid, "email": name} for uid, name in users]},
                      "streamSettings": {"network": "ws", "security": "none",
                                         "wsSettings": {"path": RELAY_PATH}}}],
        "outbounds": [{"tag": "node", "protocol": "vless",
                       "settings": {"vnext": [{"address": node["host"], "port": int(node["port"]),
                                               "users": [{"id": relay_uuid(settings.SECRET_KEY, server_id),
                                                          "encryption": "none", "flow": "xtls-rprx-vision"}]}]},
                       "streamSettings": {"network": "tcp", "security": "reality",
                                          "realitySettings": {"serverName": node["sni"],
                                                              "fingerprint": "chrome",
                                                              "password": node["public_key"],
                                                              "shortId": node["short_id"],
                                                              "spiderX": ""}}}],
        "routing": {"domainStrategy": "AsIs", "rules": []},
    }


async def sync() -> dict:
    async with async_session_maker() as db:
        servers = (await db.execute(select(VpnServer).where(
            VpnServer.provider != "private-line-primary",
            VpnServer.status.in_([VpnServerStatus.active, VpnServerStatus.maintenance]),
        ).order_by(VpnServer.id))).scalars().all()
        server = next((row for row in servers if row.status == VpnServerStatus.active
                       and _server_protocol(row) == "direct_xray"), None)
        if server is None:
            server = next((row for row in servers if _server_protocol(row) == "direct_xray"), None)
        if server is None:
            raise RuntimeError("No active direct Xray node for HTTPS relay")
        keys = (await db.execute(select(VpnKey, VpnServer).join(
            Subscription, Subscription.id == VpnKey.subscription_id).join(
            VpnServer, VpnServer.id == VpnKey.server_id).where(
            VpnKey.status == VpnKeyStatus.active,
            VpnServer.status != VpnServerStatus.disabled,
            Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.expires_soon]),
            Subscription.expires_at > datetime.utcnow(),
        ).order_by(VpnKey.id))).all()
        users = []
        seen = set()
        for key, key_server in keys:
            if _server_protocol(key_server) != "direct_xray":
                continue
            uid = str(UUID(urlsplit(decrypt(key.access_url_encrypted)).username or ""))
            if uid not in seen:
                users.append((uid, f"key-{key.id}"))
                seen.add(uid)
        node = json.loads(decrypt(server.api_secret_encrypted))
        config = json.dumps(render(users, node, server.id), separators=(",", ":"), sort_keys=True) + "\n"
    RELAY_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = RELAY_DIR / "config.json"
    if path.exists() and path.read_text() == config:
        return {"changed": False, "users": len(users), "server_id": server.id}
    fd, tmp = tempfile.mkstemp(prefix="config.", dir=RELAY_DIR)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(config)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    (RELAY_DIR / "healthy").unlink(missing_ok=True)
    return {"changed": True, "users": len(users), "server_id": server.id}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(sync())))
