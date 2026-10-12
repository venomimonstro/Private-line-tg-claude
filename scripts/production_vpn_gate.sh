#!/usr/bin/env bash
# Private Line production acceptance gate.
# Usage on CONTROL PLANE: bash scripts/production_vpn_gate.sh 3
#
# This script does NOT install a VPN node on the control-plane VPS and does NOT
# rotate paid-user UUIDs or Reality keys.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

CURRENT_STEP="bootstrap"
on_error() {
    rc=$?
    echo >&2
    echo "================ PRODUCTION FAIL ================" >&2
    echo "Step: $CURRENT_STEP" >&2
    echo "Exit code: $rc" >&2
    echo "Recent control-plane errors (credentials redacted):" >&2
    docker compose logs --tail 80 admin bot 2>&1 |         grep -Ei 'error|exception|traceback|failed|critical|panic' |         sed -E           -e 's/[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/[UUID]/g'           -e 's/(password|token|secret|privateKey)[=:][^[:space:]]+/\1=[REDACTED]/Ig'           -e 's@vless://[^[:space:]<]+@[VLESS_REDACTED]@g' |         tail -n 30 >&2 || true
    echo "Do not run reinstall/fix_reality. Correct this failed step first." >&2
    exit "$rc"
}
trap on_error ERR

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

CURRENT_STEP="1/11 control-plane resources"; echo "=== 1/11 Resource-safe control plane ==="
# Acceptance must never restart or recreate production services.
# Deployment is a separate, sequential low-memory operation.
docker ps --format '{{.Names}}' | grep -qx 'private-line-db-1' || fail "database container is not running"
docker ps --format '{{.Names}}' | grep -qx 'private-line-admin-1' || fail "admin container is not running"
docker ps --format '{{.Names}}' | grep -qx 'private-line-bot-1' || fail "bot container is not running"
docker ps --format '{{.Names}}' | grep -qx 'private-line-nginx-1' || fail "nginx container is not running"
curl -q -fsS --max-time 5 http://127.0.0.1:8000/_health >/dev/null || fail "FastAPI admin is unhealthy"

CURRENT_STEP="2/11 database migrations"; echo "=== 2/11 Database migrations ==="
docker compose exec -T admin python -m alembic upgrade head
docker compose exec -T admin python -m alembic current

CURRENT_STEP="3/11 preflight"; echo "=== 3/11 Static/release preflight ==="
bash scripts/preflight_release.sh

echo "Production gate is validation-only for long-running control-plane services."
docker compose ps admin bot db nginx

CURRENT_STEP="4/11 control-plane integration"; echo "=== 4/11 Control-plane integration ==="
docker compose exec -T admin python -m app.verify_control_plane

CURRENT_STEP="5/11 preserve-first reconciliation"; echo "=== 5/11 Preserve-first VPN fleet reconciliation ==="
docker compose exec -T admin python -m app.stabilize_vpn_fleet --apply

CURRENT_STEP="6/11 data integrity"; echo "=== 6/11 Central data invariants ==="
docker compose exec -T admin python -m app.audit_data_integrity

CURRENT_STEP="7/11 disposable-user end-to-end"; echo "=== 7/11 Disposable-user lifecycle on every dedicated VPN node ==="
NODE_RAW="$(docker compose exec -T admin python -m app.production_probe_targets)"     || fail "No dedicated VPN nodes"
mapfile -t NODE_IDS <<<"$NODE_RAW"
[[ "${#NODE_IDS[@]}" -gt 0 ]] || fail "No VPN node IDs returned"
for server_id in "${NODE_IDS[@]}"; do
    [[ "$server_id" =~ ^[1-9][0-9]*$ ]] || fail "Invalid node ID from database"
    echo "--- disposable smoke: server $server_id ---"
    if ! bash scripts/smoke_vpn_node.sh "$server_id"; then
        docker compose exec -T admin python -m app.node_probe_state \
            --server "$server_id" --failure \
            --reason "production gate disposable VLESS Reality HTTPS probe failed" \
            >/dev/null 2>&1 || true
        fail "Disposable tunnel failed on server $server_id"
    fi
done

CURRENT_STEP="8/11 node integrity"; echo "=== 8/11 Deep read-only node audit after publication ==="
docker compose exec -T admin python -m app.audit_vpn_nodes

CURRENT_STEP="9/11 published subscription"; echo "=== 9/11 Published Telegram/subscription profile ==="
docker compose exec -T admin python -m app.verify_vpn_delivery     --subscription "$SUB_ID"

CURRENT_STEP="10/11 paid-key VLESS egress"; echo "=== 10/11 Existing paid key through every node it uses ==="
SUB_NODE_RAW="$(docker compose exec -T admin python -m app.subscription_probe_targets     --subscription "$SUB_ID")" || fail "Subscription $SUB_ID has no verified active key"
mapfile -t SUB_NODE_IDS <<<"$SUB_NODE_RAW"
for server_id in "${SUB_NODE_IDS[@]}"; do
    [[ "$server_id" =~ ^[1-9][0-9]*$ ]] || fail "Invalid subscription node ID"
    echo "--- paid-key E2E: subscription $SUB_ID / server $server_id ---"
    if ! bash scripts/probe_vless_e2e.sh "$SUB_ID" "$server_id"; then
        docker compose exec -T admin python -m app.node_probe_state \
            --server "$server_id" --failure \
            --reason "production gate paid customer VLESS Reality HTTPS probe failed" \
            >/dev/null 2>&1 || true
        fail "Paid-key tunnel failed on server $server_id"
    fi
    docker compose exec -T admin python -m app.node_probe_state \
        --server "$server_id" --success
done

CURRENT_STEP="11/11 post-test invariants"; echo "=== 11/11 Post-test invariants and fatal-log scan ==="
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
