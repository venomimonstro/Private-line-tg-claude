#!/usr/bin/env bash
# Release health checks run on the MAIN server after git pull / service restart.
# This does not alter subscriptions, Reality credentials, or VPN node configs.
# Usage: bash scripts/smoke_private_line.sh 3
set -euo pipefail
cd "$(dirname "$0")/.."
SUB="${1:-3}"
[[ "$SUB" =~ ^[1-9][0-9]*$ ]] || { echo "Usage: $0 SUBSCRIPTION_ID" >&2; exit 2; }

echo "=== Python syntax ==="
python3 -m compileall -q app bot admin
echo "PASS: Python code compiles"

echo "=== Client profile format ==="
python3 -m unittest discover -s tests -p 'test_vpn_link.py' -q
echo "PASS: canonical Private Line VPN profile tests"

echo "=== Docker services ==="
docker compose ps admin bot db nginx

echo "=== Admin backend ==="
curl -q -fsS --max-time 8 http://127.0.0.1:8000/_health >/dev/null
curl -q -fsS --max-time 8 http://127.0.0.1:29148/admin/login >/dev/null
echo "PASS: backend and admin proxy HTTP 200"

echo "=== VPN node APIs / public hosts ==="
docker compose exec -T admin python -m app.audit_vpn_nodes

echo "=== Telegram and published subscription URL ==="
docker compose exec -T admin python -m app.verify_vpn_delivery --subscription "$SUB"

echo "=== Live VLESS Reality egress ==="
bash scripts/probe_vless_e2e.sh "$SUB"

echo "=== RESULT ==="
echo "PASS: server-side stack and VLESS egress for subscription $SUB"
echo "NOT COVERED: real customer ISP routing, VPN app settings, device DNS."
