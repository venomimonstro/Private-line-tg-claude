from __future__ import annotations
import argparse, asyncio
from app.database import async_session_maker
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _server_protocol
from app.vpn.direct_xray import DirectXrayClient

async def run(action:int|str, server_id:int)->int:
    async with async_session_maker() as db:
        server=await db.get(VpnServer,server_id)
        if server is None or _server_protocol(server)!="direct_xray":
            print("FAIL: direct Xray node not found")
            return 2
        name=f"plcanary{server_id}"
        client=DirectXrayClient(server)
        try:
            if action=="create":
                try:
                    key=await client.get_key(name)
                except Exception:
                    key=await client.create_key(name=name)
                print(key.access_url)
            else:
                try: await client.disable_key(name)
                except Exception: pass
                print("PASS: canary removed")
            return 0
        finally:
            await client.aclose()

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("action",choices=["create","delete"])
    p.add_argument("--server",type=int,required=True)
    a=p.parse_args()
    raise SystemExit(asyncio.run(run(a.action,a.server)))
