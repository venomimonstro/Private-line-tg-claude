#!/usr/bin/env bash
# Real external VPN data-plane + provisioning watchdog.
# Runs on the control-plane host from a systemd timer.
set -Eeuo pipefail
umask 077

ROOT="/opt/private-line"
cd "$ROOT"
[[ -f .env && -d .git ]] || exit 0

# Never race an operator deployment/convergence.
exec 8>/var/lock/private-line-production-update.lock
flock -n 8 || exit 0

NODE_ROWS="$(docker compose exec -T admin python - <<'PY'
import asyncio
from sqlalchemy import select
from app.database import async_session_maker
from app.enums import VpnServerStatus
from app.models.vpn_server import VpnServer
from app.services.vpn_key import _server_protocol

async def main():
    async with async_session_maker() as db:
        rows = (await db.execute(
            select(VpnServer)
            .where(
                VpnServer.status.in_([
                    VpnServerStatus.active,
                    VpnServerStatus.unreachable,
                ]),
                VpnServer.provider != "private-line-primary",
            )
            .order_by(VpnServer.id.asc())
        )).scalars().all()
        for server in rows:
            if _server_protocol(server) == "marzban":
                print(f"{server.id}\t{server.status.value}")

asyncio.run(main())
PY
)" || exit 0

[[ -n "$NODE_ROWS" ]] || exit 0

STATE_DIR="$ROOT/data/vpn-health"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
HOT_ADD_INTERVAL=1800
FAILED=0

while IFS=$'\t' read -r SERVER_ID SERVER_STATUS; do
    [[ "$SERVER_ID" =~ ^[1-9][0-9]*$ ]] || continue
    [[ -n "$SERVER_STATUS" ]] || continue

    echo "=== VPN WATCH node $SERVER_ID status=$SERVER_STATUS ==="

    NODE_OK=1
    NEED_HOT_ADD=0
    HOT_FILE="$STATE_DIR/hot-add-$SERVER_ID.epoch"
    NOW="$(date +%s)"
    LAST=0

    if [[ -s "$HOT_FILE" ]]; then
        read -r LAST < "$HOT_FILE" || LAST=0
        [[ "$LAST" =~ ^[0-9]+$ ]] || LAST=0
    fi

    # Any quarantined node must prove live user mutation before readmission.
    # Healthy nodes repeat the mutation proof every 30 minutes.
    if [[ "$SERVER_STATUS" != "active" || $((NOW - LAST)) -ge "$HOT_ADD_INTERVAL" ]]; then
        NEED_HOT_ADD=1
    fi

    if [[ "$NEED_HOT_ADD" -eq 1 ]]; then
        echo "[watch] Fresh-user hot-add + hot-remove without Xray restart"
        if bash scripts/smoke_vpn_node.sh "$SERVER_ID" --hot-add; then
            if docker compose exec -T admin python -m app.node_probe_state \
                --server "$SERVER_ID" --hot-add-success >/dev/null; then
                printf '%s\n' "$NOW" > "$HOT_FILE"
                chmod 600 "$HOT_FILE"
            else
                NODE_OK=0
            fi
        else
            NODE_OK=0
        fi
    fi

    if [[ "$NODE_OK" -eq 1 ]]; then
        echo "[watch] Persistent synthetic VLESS/REALITY HTTPS E2E"
        bash scripts/smoke_vpn_node.sh "$SERVER_ID" || NODE_OK=0
    fi

    if [[ "$NODE_OK" -eq 1 ]]; then
        docker compose exec -T admin python -m app.node_probe_state \
            --server "$SERVER_ID" --success >/dev/null || NODE_OK=0
    fi

    if [[ "$NODE_OK" -eq 1 ]]; then
        echo "PASS node $SERVER_ID: data-plane and provisioning healthy"
    else
        FAILED=$((FAILED + 1))
        docker compose exec -T admin python -m app.node_probe_state \
            --server "$SERVER_ID" --failure \
            --reason "scheduled VPN E2E or zero-restart provisioning probe failed" \
            >/dev/null 2>&1 || true
        echo "FAIL node $SERVER_ID: quarantined for new assignments" >&2
    fi
done <<< "$NODE_ROWS"

if (( FAILED > 0 )); then
    docker compose exec -T admin python - <<PY >/dev/null 2>&1 || true
import asyncio
from app.services.notification import notify_admin
asyncio.run(notify_admin(
    "🔴 VPN health-watch: failed nodes: $FAILED. "
    "New assignments stopped; existing credentials preserved."
))
PY
    exit 1
fi

exit 0
