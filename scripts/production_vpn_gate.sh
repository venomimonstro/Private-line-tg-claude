#!/usr/bin/env bash
# Private Line production acceptance gate.
# Usage on CONTROL PLANE: bash scripts/production_vpn_gate.sh 3
#
# This script does NOT install a VPN node on the control-plane VPS and does NOT
# rotate paid-user UUIDs or Reality keys.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

SUB_ID="${1:-3}"
[[ "$SUB_ID" =~ ^[1-9][0-9]*$ ]] || {
    echo "Usage: bash scripts/production_vpn_gate.sh SUBSCRIPTION_ID" >&2
    exit 2
}
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

fail() {
    echo
    echo "================ PRODUCTION FAIL ================"
    echo "$*" >&2
    exit 2
}

echo "=== 1/10 Resource-safe control plane ==="
# Monitoring is optional on the 1GB VPS and must never compete with the VPN
# control plane. Stop it even if it was started before profiles were introduced.
docker compose --profile monitoring stop     grafana prometheus alertmanager node-exporter >/dev/null 2>&1 || true
docker compose up -d db admin bot nginx
for _ in $(seq 1 30); do
    if curl -q -fsS --max-time 3 http://127.0.0.1:8000/_health >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
curl -q -fsS --max-time 5 http://127.0.0.1:8000/_health >/dev/null     || fail "FastAPI admin did not become healthy"

echo "=== 2/10 Static/release preflight ==="
bash scripts/preflight_release.sh

echo "=== 3/10 Control-plane integration ==="
docker compose exec -T admin python -m app.verify_control_plane

echo "=== 4/10 Preserve-first VPN fleet reconciliation ==="
docker compose exec -T admin python -m app.stabilize_vpn_fleet --apply

echo "=== 5/10 Central data invariants ==="
docker compose exec -T admin python -m app.audit_data_integrity

echo "=== 6/10 Deep read-only node audit ==="
docker compose exec -T admin python -m app.audit_vpn_nodes

echo "=== 7/10 Disposable-user lifecycle on every dedicated VPN node ==="
NODE_RAW="$(docker compose exec -T admin python -m app.production_probe_targets)"     || fail "No verified dedicated VPN nodes"
mapfile -t NODE_IDS <<<"$NODE_RAW"
[[ "${#NODE_IDS[@]}" -gt 0 ]] || fail "No VPN node IDs returned"
for server_id in "${NODE_IDS[@]}"; do
    [[ "$server_id" =~ ^[1-9][0-9]*$ ]] || fail "Invalid node ID from database"
    echo "--- disposable smoke: server $server_id ---"
    bash scripts/smoke_vpn_node.sh "$server_id"
done

echo "=== 8/10 Published Telegram/subscription profile ==="
docker compose exec -T admin python -m app.verify_vpn_delivery     --subscription "$SUB_ID"

echo "=== 9/10 Existing paid key through every node it uses ==="
SUB_NODE_RAW="$(docker compose exec -T admin python -m app.subscription_probe_targets     --subscription "$SUB_ID")" || fail "Subscription $SUB_ID has no verified active key"
mapfile -t SUB_NODE_IDS <<<"$SUB_NODE_RAW"
for server_id in "${SUB_NODE_IDS[@]}"; do
    [[ "$server_id" =~ ^[1-9][0-9]*$ ]] || fail "Invalid subscription node ID"
    echo "--- paid-key E2E: subscription $SUB_ID / server $server_id ---"
    bash scripts/probe_vless_e2e.sh "$SUB_ID" "$server_id"
done

echo "=== 10/10 Post-test invariants and fatal-log scan ==="
docker compose exec -T admin python -m app.audit_data_integrity
docker compose exec -T admin python -m app.audit_vpn_nodes
docker compose exec -T admin python -m app.verify_control_plane

FATAL_LOGS="$(docker compose logs --since "$STARTED_AT" admin bot 2>&1 |     grep -E 'Application startup failed|ModuleNotFoundError|SyntaxError|ImportError:|OutOfMemory|Killed process|segmentation fault' || true)"
if [[ -n "$FATAL_LOGS" ]]; then
    echo "$FATAL_LOGS" | tail -n 30
    fail "Fatal application errors appeared during the gate"
fi

echo
echo "=================================================="
echo "PRODUCTION PASS"
echo "Control plane: PASS"
echo "VPN fleet integrity: PASS"
echo "Disposable user lifecycle + real HTTPS tunnel: PASS"
echo "Paid subscription $SUB_ID + published Private Line VPN profile: PASS"
echo "=================================================="
echo "Remaining external check: connect once from the actual customer phone/ISP."
