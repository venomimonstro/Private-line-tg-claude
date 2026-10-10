#!/usr/bin/env bash
# Real VPN handshake/egress test from the MAIN server through a temporary
# Xray client to a registered VLESS REALITY node. Does not modify production.
# Usage: bash scripts/probe_vless_e2e.sh 2
set -euo pipefail
umask 077

SUB_ID="${1:-}"
[[ "$SUB_ID" =~ ^[1-9][0-9]*$ ]] || {
    echo "Usage: bash scripts/probe_vless_e2e.sh SUBSCRIPTION_ID" >&2
    exit 2
}
cd "$(dirname "$0")/.."

XRAY_IMAGE="ghcr.io/xtls/xray-core:26.9.9"
SOCKS_PORT=19580
PROBE_NAME="private-line-vless-probe-$$"
WORKDIR="$(mktemp -d -t private-line-vless-XXXXXXXX)"
CONFIG="$WORKDIR/xray-client.json"

cleanup() {
    docker rm -f "$PROBE_NAME" >/dev/null 2>&1 || true
    rm -f -- "$CONFIG" "$WORKDIR/egress.txt" "$WORKDIR/curl.log"
    rmdir "$WORKDIR" 2>/dev/null || true
}
trap cleanup EXIT

echo "[1/4] Checking local temporary SOCKS port..."
if ! python3 - "$SOCKS_PORT" <<'PY'
import socket, sys
port=int(sys.argv[1])
sock=socket.socket()
try:
    sock.bind(("127.0.0.1", port))
except OSError:
    print("ERROR: local SOCKS test port is busy")
    raise SystemExit(1)
finally:
    sock.close()
PY
then
    exit 1
fi

echo "[2/4] Preparing confidential Xray config for subscription $SUB_ID..."
if ! docker compose exec -T admin python -m app.vless_e2e_probe_config \
    --subscription "$SUB_ID" --socks-port "$SOCKS_PORT" >"$CONFIG"; then
    echo "No active VLESS key found under subscription $SUB_ID." >&2
    if [[ ! -e /dev/tty ]]; then
        echo "ERROR: no interactive terminal; use --stdin-url directly." >&2
        exit 2
    fi
    printf 'Paste FULL vless:// link from Telegram (input hidden): ' > /dev/tty
    VLESS_URL=""
    IFS= read -r -s VLESS_URL < /dev/tty || { echo "No input received" >&2; exit 2; }
    printf '\n' > /dev/tty
    if ! printf '%s\n' "$VLESS_URL" | docker compose exec -T admin \
        python -m app.vless_e2e_probe_config \
        --stdin-url --socks-port "$SOCKS_PORT" >"$CONFIG"; then
        unset VLESS_URL
        echo "ERROR: pasted VLESS link did not pass validation." >&2
        exit 2
    fi
    unset VLESS_URL
fi
python3 -m json.tool "$CONFIG" >/dev/null

echo "[3/4] Starting ephemeral Xray client ($XRAY_IMAGE)..."
if ! docker run -d \
    --name "$PROBE_NAME" \
    --network host \
    --user 0:0 \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    -v "$CONFIG:/tmp/vless-client.json:ro" \
    "$XRAY_IMAGE" run -c /tmp/vless-client.json >/dev/null; then
    echo "ERROR: Xray test container could not start." >&2
    exit 2
fi

READY=false
for _ in $(seq 1 20); do
    if (echo >/dev/tcp/127.0.0.1/"$SOCKS_PORT") >/dev/null 2>&1; then
        READY=true
        break
    fi
    sleep 1
done
if [[ "$READY" != true ]]; then
    echo "FAIL: Xray client SOCKS port never started."
    docker logs --tail 25 "$PROBE_NAME" 2>&1 | \
        sed -E 's/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/[UUID]/g'
    exit 1
fi

echo "[4/4] Requesting HTTPS through VLESS REALITY / SOCKS5..."
if curl -q -4 -fsS \
    --noproxy '' \
    --proxy "socks5h://127.0.0.1:$SOCKS_PORT" \
    --connect-timeout 8 \
    --max-time 25 \
    https://api.ipify.org \
    -o "$WORKDIR/egress.txt" 2>"$WORKDIR/curl.log"; then
    EXIT_IP="$(cat "$WORKDIR/egress.txt")"
    if [[ "$EXIT_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
        echo "PASS: real VPN tunnel works end-to-end!"
        echo "VPN exit IPv4: $EXIT_IP"
        echo "If your app still cannot connect, investigate the client app, its imported profile and your local network."
        exit 0
    fi
fi

echo "FAIL: HTTPS through real VLESS REALITY tunnel did not succeed."
echo "Curl diagnostics:"
tail -n 10 "$WORKDIR/curl.log" | \
    sed -E 's/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/[UUID]/g'
echo "Temporary Xray client logs:"
docker logs --tail 35 "$PROBE_NAME" 2>&1 | \
    sed -E 's/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/[UUID]/g'
echo "Next: check Xray server logs on node 185.236.25.152, and outbound access from the node."
exit 1
