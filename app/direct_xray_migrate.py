"""One-shot migration of dedicated VPN nodes from Marzban to direct Xray."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from sqlalchemy import select

from app.database import async_session_maker
from app.encryption import decrypt, encrypt
from app.enums import VpnKeyStatus, VpnServerStatus
from app.models.vpn_key import VpnKey
from app.models.vpn_server import VpnServer
from app.vpn.direct_xray import DirectXrayClient
from app.services.vpn_link import canonical_client_link


def make_url(uuid: str, meta: dict) -> str:
    q=urlencode({
        "encryption":"none",
        "security":"reality",
        "sni":meta["sni"],
        "fp":"chrome",
        "pbk":meta["public_key"],
        "sid":meta["short_id"],
        "type":"tcp",
        "headerType":"none",
        "flow":"xtls-rprx-vision",
    })
    raw=f"vless://{uuid}@{meta['host']}:{int(meta['port'])}?{q}#Private%20Line%20VPN"
    return canonical_client_link(raw,"Private Line VPN")


async def migrate(server_id: int) -> int:
    async with async_session_maker() as db:
        server=await db.get(VpnServer,server_id)
        if server is None:
            print(f"FAIL: server {server_id} not found")
            return 2
        if server.provider=="private-line-primary":
            print(f"SKIP: server {server_id} is control-plane")
            return 0
        if not server.ssh_configured:
            print(f"FAIL: server {server_id} has no SSH access")
            return 2

        keys=(await db.execute(
            select(VpnKey).where(
                VpnKey.server_id==server.id,
                VpnKey.status==VpnKeyStatus.active,
            ).order_by(VpnKey.id)
        )).scalars().all()

        users: dict[str,str]={}
        for key in keys:
            try:
                raw=decrypt(key.access_url_encrypted)
                uid=str(UUID(urlsplit(raw).username or ""))
            except Exception:
                print(f"FAIL: key {key.id} has invalid stored VLESS UUID")
                return 2
            name=(key.outline_key_id or f"k{key.id}")[:64]
            users[name]=uid

        client=DirectXrayClient(server)
        try:
            meta=await client.sync_users(users)
            status=await client.status()
        finally:
            await client.aclose()

        if not status.get("service_active") or not status.get("listener"):
            print(f"FAIL: direct Xray node {server.id} is not listening")
            return 2

        for key in keys:
            name=(key.outline_key_id or f"k{key.id}")[:64]
            uid=users[name]
            key.outline_key_id=name
            key.access_url_encrypted=encrypt(make_url(uid,meta))

        server.provider="direct-xray"
        server.api_url=f"direct-xray://{meta['host']}:{int(meta['port'])}"
        server.api_secret_encrypted=encrypt(json.dumps({
            "protocol":"direct_xray",
            "host":meta["host"],
            "port":int(meta["port"]),
            "sni":meta["sni"],
            "public_key":meta["public_key"],
            "short_id":meta["short_id"],
        },separators=(",",":")))
        # Migration only proves durable state + local listener. Keep admission
        # closed until update_production.sh passes the external REALITY/HTTPS
        # canary and (when present) an existing paid-key E2E.
        server.status=VpnServerStatus.maintenance
        server.is_selectable=False
        server.failure_count=0
        server.last_error="Direct Xray synchronized; external E2E acceptance pending"
        server.last_error_at=None
        server.last_hot_add_success_at=None
        server.last_e2e_success_at=None
        await db.commit()
        print(
            f"DIRECT_XRAY_MIGRATION_PASS server={server.id} users={len(users)} "
            f"endpoint={meta['host']}:{meta['port']}"
        )
        return 0


async def main(server_id: int | None) -> int:
    async with async_session_maker() as db:
        if server_id:
            ids=[server_id]
        else:
            ids=list((await db.execute(
                select(VpnServer.id).where(
                    VpnServer.provider!="private-line-primary"
                ).order_by(VpnServer.id)
            )).scalars().all())
    for sid in ids:
        rc=await migrate(int(sid))
        if rc:
            return rc
    return 0


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--server",type=int)
    a=p.parse_args()
    raise SystemExit(asyncio.run(main(a.server)))
