#!/usr/bin/env bash
# Read-only standalone Xray watchdog. No user creation, no restart, no Marzban.
set -Eeuo pipefail
umask 077

ROOT="/opt/private-line"
cd "$ROOT"
[[ -f .env && -d .git ]] || exit 0

exec 8>/var/lock/private-line-production-update.lock
flock -n 8 || exit 0

NODE_ROWS="$(docker compose exec -T admin python - <<'PY'
import asyncio
from sqlalchemy import select
from app.database import async_session_maker
from app.enums import VpnServerStatus, VpnKeyStatus
from app.models.vpn_server import VpnServer
from app.models.vpn_key import VpnKey
from app.services.vpn_key import _server_protocol

async def main():
    async with async_session_maker() as db:
        servers=(await db.execute(
            select(VpnServer).where(
                VpnServer.status.in_([
                    VpnServerStatus.active,
                    VpnServerStatus.unreachable,
                ]),
                VpnServer.provider!="private-line-primary",
            ).order_by(VpnServer.id)
        )).scalars().all()
        for server in servers:
            if _server_protocol(server)!="direct_xray":
                continue
            sub=(await db.execute(
                select(VpnKey.subscription_id).where(
                    VpnKey.server_id==server.id,
                    VpnKey.status==VpnKeyStatus.active,
                ).order_by(VpnKey.id).limit(1)
            )).scalar_one_or_none()
            print(f"{server.id}\t{server.status.value}\t{sub or 0}")

asyncio.run(main())
PY
)" || exit 0

[[ -n "$NODE_ROWS" ]] || exit 0
FAILED=0

while IFS=$'\t' read -r SERVER_ID SERVER_STATUS SUB_ID; do
    [[ "$SERVER_ID" =~ ^[1-9][0-9]*$ ]] || continue
    echo "=== DIRECT XRAY WATCH node $SERVER_ID status=$SERVER_STATUS ==="
    NODE_OK=1

    if [[ "$SUB_ID" =~ ^[1-9][0-9]*$ ]]; then
        echo "[watch] Read-only paid VLESS/REALITY HTTPS E2E"
        bash scripts/probe_vless_e2e.sh "$SUB_ID" "$SERVER_ID" || NODE_OK=0
    else
        echo "[watch] No paid key yet; verify standalone Xray service/listener over SSH"
        docker compose exec -T admin python - "$SERVER_ID" <<'PY' >/dev/null || NODE_OK=0
import asyncio, sys
from app.database import async_session_maker
from app.models.vpn_server import VpnServer
from app.vpn.direct_xray import DirectXrayClient

async def main():
    async with async_session_maker() as db:
        server=await db.get(VpnServer,int(sys.argv[1]))
        if server is None:
            raise SystemExit(2)
        client=DirectXrayClient(server)
        try:
            if not await client.check_connection():
                raise SystemExit(2)
        finally:
            await client.aclose()
asyncio.run(main())
PY
    fi

    if [[ "$NODE_OK" -eq 1 ]]; then
        docker compose exec -T admin python -m app.node_probe_state \
            --server "$SERVER_ID" --success >/dev/null || NODE_OK=0
    fi

    if [[ "$NODE_OK" -eq 1 ]]; then
        echo "PASS node $SERVER_ID"
    else
        FAILED=$((FAILED + 1))
        docker compose exec -T admin python -m app.node_probe_state \
            --server "$SERVER_ID" --failure \
            --reason "scheduled direct Xray read-only E2E failed" >/dev/null 2>&1 || true
        echo "FAIL node $SERVER_ID: quarantined for new assignments" >&2
    fi
done <<< "$NODE_ROWS"

if ! bash scripts/converge_https_fallback.sh; then
    FAILED=$((FAILED + 1))
    echo "FAIL: HTTPS fallback is unavailable; primary node remains unchanged" >&2
fi

if (( FAILED > 0 )); then
    docker compose exec -T admin python - <<PY >/dev/null 2>&1 || true
import asyncio
from app.services.notification import notify_admin
asyncio.run(notify_admin(
    "🔴 Direct Xray health-watch: failed nodes: $FAILED. "
    "New assignments stopped; existing keys preserved.",
    dedupe_key="direct-xray-health-watch-failure",
    cooldown_seconds=21600,
))
PY
    exit 1
fi
