#!/usr/bin/env bash
# One-command production deployment orchestrator.
# Run only on the control-plane server from /opt/private-line.
#
# Responsibilities:
# - validate/recover the control plane;
# - build the immutable application image;
# - discover registered Marzban nodes from the production DB;
# - update node runtime over SSH only when node-side code changed (or state is unknown);
# - reconcile Marzban DB/runtime/host metadata;
# - run disposable-user and paid-key E2E probes;
# - remember the node revision only after every node passes.
set -Eeuo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

FORCE_NODES=false
for arg in "$@"; do
    case "$arg" in
        --force-nodes) FORCE_NODES=true ;;
        *) echo "Usage: bash scripts/update_production.sh [--force-nodes]" >&2; exit 2 ;;
    esac
done

fail() {
    echo "PRODUCTION UPDATE FAIL: $*" >&2
    exit 2
}

quarantine_node() {
    local server_id="$1" reason="$2"
    docker compose exec -T admin python -m app.node_probe_state \
        --server "$server_id" --failure --reason "$reason" >/dev/null 2>&1 || true
}

[[ "$(pwd)" == "/opt/private-line" ]] || {
    echo "WARN: expected /opt/private-line, current path: $(pwd)" >&2
}
[[ -d .git ]] || fail "git checkout is missing"
[[ -f .env ]] || fail ".env is missing"
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

exec 9>/var/lock/private-line-production-update.lock
flock -n 9 || fail "another production update is already running"

HEAD="$(git rev-parse HEAD)"
CODE_CHANGED="${PRIVATE_LINE_CODE_CHANGED:-1}"
IMAGE_REVISION="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' private-line-app:runtime 2>/dev/null || true)"
CURRENT_IMAGE_ID="$(docker image inspect -f '{{.Id}}' private-line-app:runtime 2>/dev/null || true)"
ADMIN_CID="$(docker compose ps -q admin 2>/dev/null || true)"
BOT_CID="$(docker compose ps -q bot 2>/dev/null || true)"
ADMIN_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$ADMIN_CID" 2>/dev/null || true)"
BOT_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$BOT_CID" 2>/dev/null || true)"

echo "=== CONTROL PLANE ==="
WINDOWS_SOURCE_HASH="$(
    find windows_native -type f       ! -path '*/bin/*' ! -path '*/obj/*'       ! -path '*/out-client/*' ! -path '*/out-installer/*'       ! -path '*/Payload/*.exe' -print0 2>/dev/null     | sort -z | xargs -0 sha256sum 2>/dev/null | sha256sum | awk '{print $1}'
)"
WINDOWS_HASH_FILE="$ROOT/data/windows_dist/.source.sha256"
WINDOWS_EXE="$ROOT/data/windows_dist/PrivateLineSetup.exe"
WINDOWS_STALE=true
if [[ -s "$WINDOWS_EXE" && -s "$WINDOWS_HASH_FILE" ]]    && [[ "$(cat "$WINDOWS_HASH_FILE")" == "$WINDOWS_SOURCE_HASH" ]]    && [[ "$(head -c2 "$WINDOWS_EXE")" == "MZ" ]]; then
    WINDOWS_STALE=false
fi

if [[ "$CODE_CHANGED" == "1" || "$IMAGE_REVISION" != "$HEAD" || -z "$CURRENT_IMAGE_ID" \
   || "$ADMIN_IMAGE_ID" != "$CURRENT_IMAGE_ID" || "$BOT_IMAGE_ID" != "$CURRENT_IMAGE_ID" \
   || ( "${PRIVATE_LINE_BUILD_WINDOWS:-0}" == "1" && "$WINDOWS_STALE" == true ) ]]; then
    if [[ "$IMAGE_REVISION" != "$HEAD" || "$ADMIN_IMAGE_ID" != "$CURRENT_IMAGE_ID" \
       || "$BOT_IMAGE_ID" != "$CURRENT_IMAGE_ID" ]]; then
        echo "Running application image does not match checkout $HEAD; rebuilding before regression tests."
    fi
    if [[ "${PRIVATE_LINE_BUILD_WINDOWS:-0}" == "1" && "$WINDOWS_STALE" == true ]]; then
        echo "Windows installer is missing/stale; deployment is required."
    fi
    bash scripts/deploy_low_memory.sh
else
    RUNNING_COUNT="$(docker compose ps --status running -q db admin bot nginx | sed '/^$/d' | wc -l)"
    if curl -q -fsS --max-time 5 http://127.0.0.1:8000/_health >/dev/null 2>&1 && [[ "$RUNNING_COUNT" -eq 4 ]]; then
        echo "PASS: running admin/bot image matches checkout; all four core services are healthy"
    else
        echo "Control plane is not fully healthy; running recovery deploy."
        bash scripts/deploy_low_memory.sh
    fi
fi

# Docker images are immutable. Verify the actual containers after deployment,
# not only the image tag: an external git pull may have updated the checkout
# while the already running admin and bot still serve the previous revision.
LIVE_IMAGE_ID="$(docker image inspect -f '{{.Id}}' private-line-app:runtime 2>/dev/null || true)"
LIVE_REVISION="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' private-line-app:runtime 2>/dev/null || true)"
for service in admin bot; do
    cid="$(docker compose ps -q "$service" 2>/dev/null || true)"
    [[ -n "$cid" && -n "$LIVE_IMAGE_ID" \
       && "$(docker inspect -f '{{.Image}}' "$cid" 2>/dev/null || true)" == "$LIVE_IMAGE_ID" \
       && "$LIVE_REVISION" == "$HEAD" ]] \
        || fail "$service is not running the application image built from $HEAD"
done

echo "=== VPN REGRESSION GATE ==="
docker compose exec -T admin python -m unittest \
    tests.test_vpn_link \
    tests.test_vless_probe_config \
    tests.test_vpn_safety_guards \
    tests.test_marzban_stability \
    tests.test_node_selection \
    tests.test_marzban_runtime_patch \
    tests.test_direct_xray \
    || fail "VPN regression suite failed; nodes were not modified"
echo "PASS: VLESS regression suite"

if [[ "${PRIVATE_LINE_BUILD_WINDOWS:-0}" == "1" ]]; then
    docker compose exec -T admin python -m unittest tests.test_windows_native_installer tests.test_windows_diagnostics \
        || fail "optional Windows regression suite failed"
fi

# Install/refresh the host-level real data-plane watchdog. The current
# production-update lock prevents it from racing this rollout.
bash scripts/install_vpn_health_timer.sh

# Node-side revision state is intentionally runtime data, not git-tracked state.
STATE_FILE="$ROOT/data/.node_deploy_commit"
mkdir -p "$ROOT/data"
NODE_UPDATE=false
NODE_STATE=""

if [[ "$FORCE_NODES" == true ]]; then
    NODE_UPDATE=true
elif [[ ! -s "$STATE_FILE" ]]; then
    echo "Node deploy state is absent; one safe node convergence is required."
    NODE_UPDATE=true
else
    NODE_STATE="$(tr -d '[:space:]' < "$STATE_FILE")"
    if ! git cat-file -e "$NODE_STATE^{commit}" 2>/dev/null; then
        echo "Previous node revision is unavailable locally; convergence required."
        NODE_UPDATE=true
    elif [[ "$NODE_STATE" != "$HEAD" ]]; then
        NODE_CHANGED_FILES="$(git diff --name-only "$NODE_STATE" "$HEAD" --             install_node.sh             scripts/converge_vpn_node.sh             scripts/prepare_marzban_runtime.sh             scripts/smoke_vpn_node.sh             scripts/probe_vless_e2e.sh             scripts/inspect_node_vless.sh             app/vless_e2e_probe_config.py             app/node_smoke_user.py             app/vpn/marzban.py             app/services/vpn_reconcile.py || true)"
        if [[ -n "$NODE_CHANGED_FILES" ]]; then
            echo "Node/VPN changes since last successful node rollout:"
            printf '%s\n' "$NODE_CHANGED_FILES" | sed 's/^/  - /'
            NODE_UPDATE=true
        fi
    fi
fi

if [[ "$NODE_UPDATE" != true ]]; then
    echo "No node-side code change; running idempotent node convergence anyway to repair runtime/firewall drift."
fi
# Configuration drift (firewall, REALITY metadata, target, pinned core) can
# break customer traffic without any Git change. The node updater is designed
# to be idempotent, so always converge before the mandatory E2E acceptance.
NODE_UPDATE=true

echo
echo "=== DISCOVER REGISTERED MARZBAN NODES ==="
NODE_ROWS="$(docker compose exec -T admin python - <<'PY'
import asyncio
from urllib.parse import urlsplit
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
                VpnServer.status != VpnServerStatus.disabled,
                VpnServer.provider != "private-line-primary",
            )
            .order_by(VpnServer.id.asc())
        )).scalars().all()
        found=0
        for server in rows:
            if _server_protocol(server) != "marzban":
                continue
            host=urlsplit(server.api_url).hostname
            if not host:
                continue
            print(f"{server.id}\t{host}")
            found += 1
        if not found:
            raise SystemExit(3)

asyncio.run(main())
PY
)" || fail "could not discover a registered Marzban node"

[[ -n "$NODE_ROWS" ]] || fail "no Marzban nodes returned"
printf '%s\n' "$NODE_ROWS" | sed 's/^/  /'

REMOTE_ARCHIVE=""
CRED_ROOT="$(mktemp -d)"
chmod 700 "$CRED_ROOT"
trap '[[ -n "$REMOTE_ARCHIVE" ]] && rm -f "$REMOTE_ARCHIVE"; rm -rf "$CRED_ROOT"' EXIT

if [[ "$NODE_UPDATE" == true ]]; then
    REMOTE_ARCHIVE="$(mktemp)"
    tar -czf "$REMOTE_ARCHIVE" \
        scripts/converge_vpn_node.sh \
        scripts/prepare_marzban_runtime.sh \
        scripts/inspect_node_vless.sh \
        scripts/install_direct_xray.sh \
        node/agent.py node/migrate.py node/selftest.py
fi

while IFS=$'\t' read -r SERVER_ID NODE_HOST; do
    [[ "$SERVER_ID" =~ ^[1-9][0-9]*$ ]] || fail "invalid server id: $SERVER_ID"
    [[ -n "$NODE_HOST" ]] || fail "empty node host for server $SERVER_ID"

    echo
    echo "=== NODE $SERVER_ID / $NODE_HOST ==="

    if [[ "$NODE_UPDATE" == true ]]; then
        SSH_JSON=""
        set +e
        SSH_JSON="$(docker compose exec -T admin python -m app.node_ssh_access --server "$SERVER_ID" 2>/dev/null)"
        SSH_RC=$?
        set -e
        if [[ "$SSH_RC" -ne 0 ]]; then
            fail "SSH access is not configured for node $SERVER_ID. Open Admin -> VPN-серверы -> SSH-доступ and save it once."
        fi

        mapfile -t SSH_FIELDS < <(
            SSH_JSON_VALUE="$SSH_JSON" python3 -c '
import json, os
data=json.loads(os.environ["SSH_JSON_VALUE"])
for key in ("host","port","username","auth_type","secret_b64"):
    value=data.get(key)
    if value in (None, ""):
        raise SystemExit(2)
    print(value)
'
        )
        [[ "${#SSH_FIELDS[@]}" -eq 5 ]] || fail "invalid stored SSH access for node $SERVER_ID"
        SSH_HOST="${SSH_FIELDS[0]}"
        SSH_PORT="${SSH_FIELDS[1]}"
        SSH_USER="${SSH_FIELDS[2]}"
        SSH_AUTH="${SSH_FIELDS[3]}"
        SSH_SECRET_B64="${SSH_FIELDS[4]}"
        unset SSH_FIELDS SSH_JSON

        [[ "$SSH_PORT" =~ ^[1-9][0-9]*$ && "$SSH_PORT" -le 65535 ]] \
            || fail "invalid SSH port stored for node $SERVER_ID"

        NODE_CRED_DIR="$CRED_ROOT/node-$SERVER_ID"
        mkdir -m 700 "$NODE_CRED_DIR"
        SECRET_FILE="$NODE_CRED_DIR/secret"
        printf '%s' "$SSH_SECRET_B64" | base64 -d > "$SECRET_FILE" \
            || fail "cannot decode stored SSH secret for node $SERVER_ID"
        unset SSH_SECRET_B64
        chmod 600 "$SECRET_FILE"

        SSH_COMMON=(
            -p "$SSH_PORT"
            -o ConnectTimeout=10
            -o ServerAliveInterval=15
            -o ServerAliveCountMax=3
            -o StrictHostKeyChecking=accept-new
            -o NumberOfPasswordPrompts=1
        )
        # Credential payload is never logged and is removed by the EXIT trap.
        docker compose exec -T admin python -m app.node_management_credentials \
            --server "$SERVER_ID" > "$NODE_CRED_DIR/node-credentials.json" \
            || fail "cannot prepare node API credentials"
        tar -czf "$REMOTE_ARCHIVE" \
            scripts/converge_vpn_node.sh scripts/prepare_marzban_runtime.sh \
            scripts/inspect_node_vless.sh scripts/install_direct_xray.sh \
            node/agent.py node/migrate.py node/selftest.py \
            -C "$NODE_CRED_DIR" node-credentials.json
        REMOTE_COMMAND='set -Eeuo pipefail
             tmp="$(mktemp -d /var/tmp/private-line-rollout-XXXXXXXX)"
             trap '"'"'rm -rf "$tmp"'"'"' EXIT
             tar -xzf - -C "$tmp"
             if ! MAIN_SERVER_IP=194.146.223.104 bash "$tmp/scripts/install_direct_xray.sh"; then
                 echo "=== AUTOMATIC NODE DIAGNOSTICS: GOLDEN CONVERGENCE FAILED ===" >&2
                 bash "$tmp/scripts/inspect_node_vless.sh" >&2 || true
                 exit 1
             fi
             echo "REMOTE NODE PASS"'

        docker compose exec -T admin python -m app.node_probe_state \
            --server "$SERVER_ID" --maintenance \
            || fail "cannot pause new assignments before node migration"
        echo "[1/4] Remote node convergence (stored SSH access; no terminal prompt)"
        if [[ "$SSH_AUTH" == "private_key" ]]; then
            cat "$REMOTE_ARCHIVE" | ssh "${SSH_COMMON[@]}" \
                -o BatchMode=yes -i "$SECRET_FILE" \
                "$SSH_USER@$SSH_HOST" "$REMOTE_COMMAND" \
                || fail "remote management convergence failed for node $SERVER_ID ($SSH_HOST); node remains in planned maintenance until external VPN acceptance passes"
        elif [[ "$SSH_AUTH" == "password" ]]; then
            ASKPASS="$NODE_CRED_DIR/askpass"
            cat > "$ASKPASS" <<EOF
#!/bin/sh
cat "$SECRET_FILE"
EOF
            chmod 700 "$ASKPASS"
            command -v setsid >/dev/null 2>&1 \
                || fail "setsid is required for non-interactive SSH password authentication"
            cat "$REMOTE_ARCHIVE" | \
                DISPLAY=:private-line \
                SSH_ASKPASS_REQUIRE=force \
                SSH_ASKPASS="$ASKPASS" \
                setsid -w ssh "${SSH_COMMON[@]}" \
                    -o BatchMode=no \
                    -o PreferredAuthentications=password,keyboard-interactive \
                    -o PubkeyAuthentication=no \
                    "$SSH_USER@$SSH_HOST" "$REMOTE_COMMAND" \
                || fail "remote management convergence failed for node $SERVER_ID ($SSH_HOST); node remains in planned maintenance until external VPN acceptance passes"
        else
            fail "unsupported SSH auth type for node $SERVER_ID"
        fi
        rm -rf "$NODE_CRED_DIR"
        unset SECRET_FILE SSH_HOST SSH_PORT SSH_USER SSH_AUTH REMOTE_COMMAND
    fi

    echo "[2/5] Reconcile user UUIDs and public VLESS endpoint"
    docker compose exec -T admin python -m app.stabilize_vless --node "$NODE_HOST" \
        || { quarantine_node "$SERVER_ID" "Marzban/runtime reconciliation failed"; fail "reconciliation failed for node $SERVER_ID"; }

    echo "[3/5] Fresh-user hot-add E2E WITHOUT Xray restart"
    bash scripts/smoke_vpn_node.sh "$SERVER_ID" --hot-add \
        || { quarantine_node "$SERVER_ID" "fresh Marzban user did not hot-add into live Xray"; fail "zero-restart user provisioning failed for node $SERVER_ID"; }
    docker compose exec -T admin python -m app.node_probe_state \
        --server "$SERVER_ID" --hot-add-success \
        || fail "could not persist hot-add health proof for node $SERVER_ID"

    echo "[4/5] Persistent synthetic real HTTPS tunnel"
    bash scripts/smoke_vpn_node.sh "$SERVER_ID" \
        || { quarantine_node "$SERVER_ID" "synthetic VLESS Reality HTTPS probe failed"; fail "synthetic E2E smoke failed for node $SERVER_ID"; }

    echo "[5/5] Existing paid-key E2E when available"
    set +e
    SUB_ID="$(docker compose exec -T admin python -m app.stabilize_vless \
        --node "$NODE_HOST" --probe-subscription 2>/dev/null | tr -d '\r' | tail -n 1)"
    SUB_RC=$?
    set -e
    if [[ "$SUB_RC" -eq 0 && "$SUB_ID" =~ ^[1-9][0-9]*$ ]]; then
        if ! bash scripts/probe_vless_e2e.sh "$SUB_ID" "$SERVER_ID"; then
            echo "Paid key failed while fresh-user + synthetic probes passed."
            echo "Running ONE emergency DB->live Xray resync for historical runtime drift..."
            docker compose exec -T admin python -m app.resync_vpn_node \
                --server "$SERVER_ID" --apply \
                || { quarantine_node "$SERVER_ID" "emergency Xray DB-user resync failed"; fail "paid-key recovery resync failed on node $SERVER_ID"; }
            sleep 2
            bash scripts/probe_vless_e2e.sh "$SUB_ID" "$SERVER_ID" \
                || { quarantine_node "$SERVER_ID" "paid key still failed after one DB-user resync"; fail "paid-key E2E failed after bounded recovery: subscription $SUB_ID / node $SERVER_ID"; }
            echo "PASS: historical paid key recovered after one bounded Xray DB-user resync."
        fi
    else
        echo "No active paid key on node $SERVER_ID; fresh-user + synthetic E2E is sufficient."
    fi

    docker compose exec -T admin python -m app.node_probe_state \
        --server "$SERVER_ID" --success \
        || fail "could not publish healthy node $SERVER_ID after successful E2E"
    echo "PASS: node $SERVER_ID ($NODE_HOST)"
done <<< "$NODE_ROWS"

echo "=== RECOVER PAID ORDERS AFTER VPN ACCEPTANCE ==="
docker compose exec -T admin python - <<'PYRECOVER'
import asyncio
from app.database import async_session_maker
from app.payments.services import recover_paid_unfulfilled_payments
async def main():
    async with async_session_maker() as db:
        result = await recover_paid_unfulfilled_payments(db, limit=50)
        print("Paid order recovery:", result)
        if result["failed"]:
            raise SystemExit("FAIL: some paid orders remain unfulfilled; see admin payment errors")
asyncio.run(main())
PYRECOVER

printf '%s\n' "$HEAD" > "$STATE_FILE"
chmod 600 "$STATE_FILE"

echo
echo "========================================"
echo "PRODUCTION UPDATE PASS"
echo "Control plane: updated/healthy"
echo "Direct Xray nodes: migrated/converged from main server"
echo "VPN E2E: passed"
echo "Node revision: $HEAD"
echo "========================================"
