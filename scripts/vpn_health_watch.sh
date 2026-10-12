#!/usr/bin/env bash
# Read-mostly production VPN data-plane watchdog.
# Runs from the control-plane host via systemd timer.
set -Eeuo pipefail
umask 077

ROOT="/opt/private-line"
cd "$ROOT"
[[ -f .env && -d .git ]] || exit 0

# Never race a deployment/reconciliation. Missing one health interval is safer
# than running acceptance while the operator is intentionally changing a node.
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
        rows=(await db.execute(
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

FAILED=0
PROBE_STATE_DIR="$ROOT/data/vpn-health"
mkdir -p "$PROBE_STATE_DIR"
chmod 700 "$PROBE_STATE_DIR"
HOT_ADD_INTERVAL=1800

while IFS=
if (( FAILED > 0 )); then
    # Best-effort operator alert; health state is already persisted even if
    # Telegram is temporarily unavailable.
    docker compose exec -T admin python - <<PY >/dev/null 2>&1 || true
import asyncio
from app.services.notification import notify_admin
asyncio.run(notify_admin("🔴 VPN health-watch: failed nodes: $FAILED. New assignments were stopped; existing keys preserved."))
PY
    exit 1
fi
\t' read -r SERVER_ID SERVER_STATUS; do
    [[ "$SERVER_ID" =~ ^[1-9][0-9]*$ ]] || continue
    echo "=== VPN WATCH node $SERVER_ID status=$SERVER_STATUS ==="

    NEED_HOT_ADD=0
    HOT_FILE="$PROBE_STATE_DIR/hot-add-$SERVER_ID.epoch"
    NOW="$(date +%s)"
    LAST=0
    if [[ -s "$HOT_FILE" ]]; then
        read -r LAST < "$HOT_FILE" || LAST=0
        [[ "$LAST" =~ ^[0-9]+$ ]] || LAST=0
    fi
    if [[ "$SERVER_STATUS" != "active" || $((NOW - LAST)) -ge "$HOT_ADD_INTERVAL" ]]; then
        NEED_HOT_ADD=1
    fi

    NODE_OK=1
    if [[ "$NEED_HOT_ADD" -eq 1 ]]; then
        echo "Checking fresh-user zero-restart provisioning capability..."
        if bash scripts/smoke_vpn_node.sh "$SERVER_ID" --hot-add; then
            printf '%s\n' "$NOW" > "$HOT_FILE"
            chmod 600 "$HOT_FILE"
        else
            NODE_OK=0
        fi
    fi

    if [[ "$NODE_OK" -eq 1 ]]; then
        if ! bash scripts/smoke_vpn_node.sh "$SERVER_ID"; then
            NODE_OK=0
        fi
    fi

    if [[ "$NODE_OK" -eq 1 ]]; then
        docker compose exec -T admin python -m app.node_probe_state \
            --server "$SERVER_ID" --success >/dev/null || true
        echo "PASS node $SERVER_ID (data-plane + provisioning healthy)"
    else
        FAILED=$((FAILED + 1))
        docker compose exec -T admin python -m app.node_probe_state \
            --server "$SERVER_ID" --failure \
            --reason "scheduled VPN data-plane or fresh-user provisioning probe failed" >/dev/null || true
        echo "FAIL node $SERVER_ID quarantined for new assignments" >&2
    fi
done <<< "$NODE_ROWS"

if (( FAILED > 0 )); then
    # Best-effort operator alert; health state is already persisted even if
    # Telegram is temporarily unavailable.
    docker compose exec -T admin python - <<PY >/dev/null 2>&1 || true
import asyncio
from app.services.notification import notify_admin
asyncio.run(notify_admin("🔴 VPN health-watch: failed nodes: $FAILED. New assignments were stopped; existing keys preserved."))
PY
    exit 1
fi
