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
                VpnServer.status == VpnServerStatus.active,
                VpnServer.is_selectable.is_(True),
                VpnServer.provider != "private-line-primary",
            )
            .order_by(VpnServer.id.asc())
        )).scalars().all()
        for server in rows:
            if _server_protocol(server) == "marzban":
                print(server.id)

asyncio.run(main())
PY
)" || exit 0

[[ -n "$NODE_ROWS" ]] || exit 0

FAILED=0
while IFS= read -r SERVER_ID; do
    [[ "$SERVER_ID" =~ ^[1-9][0-9]*$ ]] || continue
    echo "=== VPN WATCH node $SERVER_ID ==="
    if bash scripts/smoke_vpn_node.sh "$SERVER_ID"; then
        docker compose exec -T admin python -m app.node_probe_state \
            --server "$SERVER_ID" --success >/dev/null || true
        echo "PASS node $SERVER_ID"
    else
        FAILED=$((FAILED + 1))
        docker compose exec -T admin python -m app.node_probe_state \
            --server "$SERVER_ID" --failure \
            --reason "scheduled real VLESS/REALITY HTTPS health probe failed" >/dev/null || true
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
