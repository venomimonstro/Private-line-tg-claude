#!/usr/bin/env bash
# Repair public IPv4 on an already-registered Marzban node without regenerating
# VPN keys, deleting users, or re-running install_node.sh.
# Usage (run on main server): bash scripts/repair_registered_node_ipv4.sh 1 185.236.25.152
set -euo pipefail

if [ "$#" -lt 2 ]; then
    echo "Usage: $0 SERVER_ID NODE_PUBLIC_IPV4 [SSH_USER@NODE_IPV4]" >&2
    exit 2
fi

SERVER_ID="$1"
NODE_IPV4="$2"
if [ "$#" -ge 3 ]; then
    SSH_TARGET="$3"
else
    SSH_TARGET="root@$NODE_IPV4"
fi

[[ "$SERVER_ID" =~ ^[0-9]+$ ]] || { echo "Invalid server ID" >&2; exit 2; }
python3 - "$NODE_IPV4" <<'PY'
import ipaddress, sys
ip = ipaddress.IPv4Address(sys.argv[1])
if not ip.is_global:
    raise SystemExit("NODE_PUBLIC_IPV4 must be a public IPv4")
PY

cd "$(dirname "$0")/.."

CURRENT_URL=$(docker compose exec -T db psql -U privateline -d privateline -At \
    -c "SELECT api_url FROM vpn_servers WHERE id=$SERVER_ID" | tr -d '\r')
if [ -z "$CURRENT_URL" ]; then
    echo "Server ID $SERVER_ID not found in main database" >&2
    exit 1
fi
echo "Current node API URL: $CURRENT_URL"
echo "Correct node API URL: http://$NODE_IPV4:2096"

# First update Marzban hosts on the node via its loopback API.
# Do not regenerate Reality key pairs or modify xray_config.json.
ssh -o ConnectTimeout=15 "$SSH_TARGET" python3 - "$NODE_IPV4" <<'PY'
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

public_ip = sys.argv[1]
config = {}
for line in Path("/opt/marzban/.env").read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        key, value = line.split("=", 1)
        config[key] = value
user = config.get("SUDO_USERNAME", "admin")
password = config.get("SUDO_PASSWORD", "")
if not password:
    raise SystemExit("Missing SUDO_PASSWORD in /opt/marzban/.env")

base = "http://127.0.0.1:8000"
auth = urllib.parse.urlencode({"username": user, "password": password}).encode()
req = urllib.request.Request(
    base + "/api/admin/token", data=auth, method="POST",
    headers={"Content-Type": "application/x-www-form-urlencoded"}
)
with urllib.request.urlopen(req, timeout=15) as response:
    access_token = json.load(response)["access_token"]
headers = {"Authorization": "Bearer " + access_token}
req = urllib.request.Request(base + "/api/hosts", headers=headers)
with urllib.request.urlopen(req, timeout=15) as response:
    hosts = json.load(response)

if not isinstance(hosts, dict) or not isinstance(hosts.get("VLESS-TCP"), list) or not hosts["VLESS-TCP"]:
    raise SystemExit("No VLESS-TCP entries in Marzban hosts; aborting without overwriting hosts")

changed = 0
for entry in hosts["VLESS-TCP"]:
    if not isinstance(entry, dict):
        raise SystemExit("Unsupported Marzban hosts entry type; aborting")
    if entry.get("address") != public_ip:
        entry["address"] = public_ip
        changed += 1

if changed:
    req = urllib.request.Request(
        base + "/api/hosts", data=json.dumps(hosts).encode(), method="PUT",
        headers={**headers, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        response.read()
print(f"Marzban hosts: {len(hosts['VLESS-TCP'])} VLESS-TCP entries, {changed} IPv4 corrections")
PY

# Update only the selected node. Keep its encrypted API credentials intact.
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U privateline -d privateline \
    -c "UPDATE vpn_servers SET api_url='http://$NODE_IPV4:2096', failure_count=0, last_error=NULL, last_error_at=NULL WHERE id=$SERVER_ID RETURNING id, name, api_url;"

# Validate auth and Reality metadata from the *main application container*.
docker compose exec -T admin python - "$SERVER_ID" <<'PY'
import asyncio
import sys

from app.database import async_session_maker
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _make_client

async def check():
    async with async_session_maker() as db:
        server = await db.get(VpnServer, int(sys.argv[1]))
        if not server:
            raise RuntimeError("Node not found")
        client = _make_client(server)
        try:
            if not await client.check_connection():
                raise RuntimeError("Marzban authentication failed")
            reality = await client.get_reality_params()
            if not reality:
                raise RuntimeError("Reality parameters unavailable")
            print("Main -> node API: OK; Reality metadata: OK")
        finally:
            await client.aclose()

asyncio.run(check())
PY

# Reset in-memory circuit breaker so failed activations can retry.
docker compose restart bot
echo "Node repaired. In admin -> VPN Servers, press 'Check'. Pending activations retry via bot scheduler."
