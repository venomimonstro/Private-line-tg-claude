#!/usr/bin/env bash
# Actual VLESS Reality smoke test for a disposable user on the primary VPS.
# Run on MAIN VPS after app.bootstrap_primary_node. Test user is deleted even
# on errors; primary is made selectable ONLY if all checks pass.
set -Eeuo pipefail
umask 077

cd "$(dirname "$0")/.."
[[ $(id -u) -eq 0 ]] || { echo "ERROR: run on main VPS as root"; exit 2; }
DIR="$(mktemp -d -t private-line-primary-smoke-XXXXXXXX)"
NAME="$(python3 -c 'import secrets; print("pltest_"+secrets.token_hex(6))')"
CONTAINER="private-line-primary-smoke-$$"
PORT=19581
CREATED=0

cleanup() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    if [[ "$CREATED" == 1 ]]; then
        docker compose exec -T admin python -m app.primary_node_smoke delete \
            --name "$NAME" >/dev/null 2>&1 || \
            echo "WARNING: temporary Marzban test user could not be removed: $NAME" >&2
    fi
    rm -f "$DIR/profile.txt" "$DIR/client.json" "$DIR/egress.txt" "$DIR/curl-errors.txt"
    rmdir "$DIR" 2>/dev/null || true
}
trap cleanup EXIT

echo "[1/4] Preparing one temporary Marzban VPN user..."
CREATED=1
if ! docker compose exec -T admin python -m app.primary_node_smoke create \
    --name "$NAME" > "$DIR/profile.txt"; then
    echo "FAIL: temporary key not created; node remains unselectable"
    exit 2
fi
if ! docker compose exec -T admin python -m app.vless_e2e_probe_config \
    --stdin-url --socks-port "$PORT" < "$DIR/profile.txt" > "$DIR/client.json"; then
    echo "FAIL: temporary key is invalid; node remains unselectable"
    exit 2
fi
python3 -m json.tool "$DIR/client.json" >/dev/null

if ! python3 - "$PORT" <<'PY'
import socket,sys
s=socket.socket()
try: s.bind(("127.0.0.1",int(sys.argv[1])))
except OSError: raise SystemExit("ERROR: temporary SOCKS port occupied")
finally: s.close()
PY
then exit 2; fi

echo "[2/4] Starting ephemeral Xray client..."
docker run -d --name "$CONTAINER" --network host --read-only \
    --cap-drop ALL --security-opt no-new-privileges \
    -v "$DIR/client.json:/tmp/client.json:ro" \
    ghcr.io/xtls/xray-core:26.9.9 run -c /tmp/client.json >/dev/null

READY=0
for i in $(seq 1 20); do
    if python3 - "$PORT" <<'PY' >/dev/null 2>&1
import socket,sys
with socket.create_connection(("127.0.0.1",int(sys.argv[1])),timeout=1) as s:
    s.sendall(b"\x05\x01\x00")
    assert s.recv(2)==b"\x05\x00"
PY
    then READY=1; break; fi
    sleep 1
done
if [[ "$READY" != 1 ]]; then
    echo "FAIL: ephemeral SOCKS test listener did not start"
    exit 2
fi

echo "[3/4] Testing Internet through the MAIN VPS Reality tunnel..."
if ! curl -q -4 -fsS --noproxy '' \
   --proxy "socks5h://127.0.0.1:$PORT" \
   --connect-timeout 8 --max-time 20 \
   https://api.ipify.org -o "$DIR/egress.txt" 2>"$DIR/curl-errors.txt"; then
    echo "FAIL: no HTTPS egress over VLESS Reality; primary node stays hidden"
    tail -n 3 "$DIR/curl-errors.txt"
    exit 1
fi
python3 - "$DIR/egress.txt" <<'PY'
import ipaddress, pathlib,sys
s=pathlib.Path(sys.argv[1]).read_text().strip()
ip=ipaddress.ip_address(s)
assert ip.is_global, "VPN exit must be globally routable"
print("PASS: Reality HTTPS egress, exit IPv4:", ip)
PY

echo "[4/4] Cleaning up test user and making verified server selectable..."
if ! docker compose exec -T admin python -m app.primary_node_smoke delete \
    --name "$NAME" >/dev/null; then
    echo "FAIL: disposable test user cleanup failed; primary remains hidden"
    exit 2
fi
CREATED=0
docker compose exec -T admin python -m app.primary_node_smoke activate
echo "PASS: primary VPN node published to Telegram"
echo "NOTE: the test was from this VPS; cellular/ISP reachability still requires a device check."
