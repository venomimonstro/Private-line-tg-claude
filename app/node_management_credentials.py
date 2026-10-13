"""Emit credentials only into the rollout's mode-0600 SSH payload."""
import argparse
import asyncio
import json
from urllib.parse import urlsplit
from app.database import async_session_maker
from app.encryption import decrypt
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _server_protocol

async def run(server_id):
    async with async_session_maker() as db:
        server = await db.get(VpnServer, server_id)
        if server is None or _server_protocol(server) != 'marzban':
            raise SystemExit('Unsupported node')
        if server.provider == 'private-line-primary':
            raise SystemExit('Control plane cannot become a VPN node')
        creds = json.loads(decrypt(server.api_secret_encrypted))
        print(json.dumps({'auth': {'username': creds['username'], 'password': creds['password']},
                          'host': urlsplit(server.api_url).hostname,
                          'control_ip': '194.146.223.104'}))

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--server', type=int, required=True)
    asyncio.run(run(p.parse_args().server))
