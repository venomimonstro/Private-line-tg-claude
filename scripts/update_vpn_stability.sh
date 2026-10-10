#!/usr/bin/env bash
# Run on the main server after pulling this revision. No node reinstall.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
NODE_IP="${1:-185.236.25.152}"
python3 - "$NODE_IP" <<'PY'
import ipaddress, sys
ipaddress.ip_address(sys.argv[1])
PY
mkdir -p backups
BACKUP="backups/pre-vpn-stability-$(date -u +%Y%m%dT%H%M%SZ)-$$.sql"
echo '[1/5] Backing up application database'
docker compose exec -T db pg_dump -U privateline -d privateline > "$BACKUP"
test -s "$BACKUP"
echo '[2/5] Building application images'
docker compose build admin bot
echo '[3/5] Pausing scheduler and updating application'
docker compose stop bot
trap 'docker compose up -d bot' EXIT
docker compose up -d --no-deps --force-recreate admin
for attempt in $(seq 1 30); do
    if docker compose exec -T admin curl -fsS http://127.0.0.1:8000/_health >/dev/null; then
        break
    fi
    if [ "$attempt" -eq 30 ]; then
        echo 'FAIL: admin did not become healthy' >&2
        exit 1
    fi
    sleep 2
done
echo '[4/5] Reconciling registered node and issued keys'
docker compose exec -T admin python -m app.stabilize_vless --node "$NODE_IP"
SUB_ID="$(docker compose exec -T admin python -m app.stabilize_vless --node "$NODE_IP" --probe-subscription)"
[[ "$SUB_ID" =~ ^[1-9][0-9]*$ ]] || { echo 'FAIL: no subscription for E2E probe'; exit 1; }
docker compose up -d --no-deps --force-recreate bot
trap - EXIT
echo '[5/5] Testing real VPN connection (one active subscription)'
bash scripts/probe_vless_e2e.sh "$SUB_ID"
