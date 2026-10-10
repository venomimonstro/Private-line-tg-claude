#!/usr/bin/env bash
# Dedicated VPN node disposable-user end-to-end test.
# Usage: bash scripts/smoke_vpn_node.sh SERVER_ID
set -Eeuo pipefail
umask 077
cd "$(dirname "$0")/.."

SERVER_ID="${1:-}"
MODE="${2:-}"
[[ "$SERVER_ID" =~ ^[1-9][0-9]*$ ]] || {
    echo "Usage: bash scripts/smoke_vpn_node.sh SERVER_ID [--publish]" >&2
    exit 2
}
[[ -z "$MODE" || "$MODE" == "--publish" ]] || {
    echo "Invalid mode: $MODE" >&2
    exit 2
}

WORK="$(mktemp -d -t private-line-node-smoke-XXXXXXXX)"
NAME="$(python3 -c 'import secrets; print("pltest_"+secrets.token_hex(8))')"
PORT=19582
CONTAINER="private-line-node-smoke-$$"
CREATED=0

cleanup() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    if [[ "$CREATED" == 1 ]]; then
        docker compose exec -T admin python -m app.node_smoke_user delete             --server "$SERVER_ID" --name "$NAME" >/dev/null 2>&1 ||             echo "WARNING: temporary node user cleanup failed: $NAME" >&2
    fi
    rm -f "$WORK/profile.txt" "$WORK/client.json" "$WORK/egress.txt" "$WORK/curl.log"
    rmdir "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

echo "[1/5] Creating disposable Marzban user on node $SERVER_ID..."
CREATED=1
docker compose exec -T admin python -m app.node_smoke_user create     --server "$SERVER_ID" --name "$NAME" > "$WORK/profile.txt"

echo "[2/5] Validating exact client share URI..."
docker compose exec -T admin python -m app.vless_e2e_probe_config     --stdin-url --socks-port "$PORT" < "$WORK/profile.txt" > "$WORK/client.json"
python3 -m json.tool "$WORK/client.json" >/dev/null

XRAY_IMAGE="${XRAY_IMAGE:-ghcr.io/xtls/xray-core:26.6.27@sha256:c98160906f3fe1d9d16e950c2a7997dc0c542c71816aa92c21e510412f90e1a2}"
echo "[3/5] Starting clean Xray compatibility client ($XRAY_IMAGE)..."
docker run -d --name "$CONTAINER" --network host --user 0:0 --read-only     --cap-drop ALL --security-opt no-new-privileges     -v "$WORK/client.json:/tmp/client.json:ro"     "$XRAY_IMAGE" run -c /tmp/client.json >/dev/null

READY=0
for _ in $(seq 1 20); do
    if python3 - "$PORT" <<'PY' >/dev/null 2>&1
import socket, sys
with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=1) as s:
    s.sendall(b"\x05\x01\x00")
    assert s.recv(2) == b"\x05\x00"
PY
    then
        READY=1
        break
    fi
    sleep 1
done
[[ "$READY" == 1 ]] || {
    echo "FAIL: temporary Xray SOCKS client did not start"
    docker logs --tail 20 "$CONTAINER" 2>&1
    exit 2
}

echo "[4/5] Requesting HTTPS through disposable VLESS Reality credentials..."
curl -q -4 -fsS --noproxy ''     --proxy "socks5h://127.0.0.1:$PORT"     --connect-timeout 8 --max-time 20     https://api.ipify.org -o "$WORK/egress.txt" 2>"$WORK/curl.log" || {
        echo "FAIL: VLESS Reality egress failed"
        tail -n 5 "$WORK/curl.log"
        docker logs --tail 25 "$CONTAINER" 2>&1
        exit 1
    }
python3 - "$WORK/egress.txt" <<'PY'
import ipaddress, pathlib, sys
value = pathlib.Path(sys.argv[1]).read_text().strip()
ip = ipaddress.ip_address(value)
assert ip.is_global
print("PASS: disposable user VPN egress IPv4:", ip)
PY

echo "[5/5] Deleting disposable Marzban user and verifying 404..."
docker compose exec -T admin python -m app.node_smoke_user delete     --server "$SERVER_ID" --name "$NAME"
CREATED=0
echo "PASS: dedicated VPN node lifecycle works end-to-end"
if [[ "$MODE" == "--publish" ]]; then
    echo "[publish] Final read-only integrity check and customer publication..."
    docker compose exec -T admin python -m app.promote_vpn_node --server "$SERVER_ID"
fi
