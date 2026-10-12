#!/usr/bin/env bash
# Dedicated VPN node disposable-user end-to-end test.
# Usage: bash scripts/smoke_vpn_node.sh SERVER_ID
set -Eeuo pipefail
umask 077
cd "$(dirname "$0")/.."

SERVER_ID="${1:-}"
MODE="${2:-}"
[[ "$SERVER_ID" =~ ^[1-9][0-9]*$ ]] || {
    echo "Usage: bash scripts/smoke_vpn_node.sh SERVER_ID [--publish|--hot-add]" >&2
    exit 2
}
[[ -z "$MODE" || "$MODE" == "--publish" || "$MODE" == "--hot-add" ]] || {
    echo "Invalid mode: $MODE" >&2
    exit 2
}

WORK="$(mktemp -d -t private-line-node-smoke-XXXXXXXX)"
HOT_ADD=0
HOT_CREATED=0
if [[ "$MODE" == "--hot-add" ]]; then
    HOT_ADD=1
    NAME="$(python3 -c 'import secrets; print("plhot_"+secrets.token_hex(8))')"
else
    NAME="plprobe_s${SERVER_ID}"
fi
PORT=19582
CONTAINER="private-line-node-smoke-$"

cleanup() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    if [[ "$HOT_CREATED" == 1 ]]; then
        docker compose exec -T admin python -m app.node_smoke_user delete-live \
            --server "$SERVER_ID" --name "$NAME" >/dev/null 2>&1 || \
            echo "WARNING: hot-add canary DB cleanup could not be confirmed" >&2
    fi
    rm -f "$WORK/profile.txt" "$WORK/client.json" "$WORK/egress.txt" "$WORK/curl.log"
    rmdir "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

if [[ "$HOT_ADD" == 1 ]]; then
    echo "[1/5] Creating fresh Marzban hot-add canary WITHOUT Xray restart..."
    docker compose exec -T admin python -m app.node_smoke_user create-live \
        --server "$SERVER_ID" --name "$NAME" > "$WORK/profile.txt"
    HOT_CREATED=1
else
    echo "[1/5] Reading persistent synthetic Marzban probe user (create once if missing)..."
    docker compose exec -T admin python -m app.node_smoke_user ensure \
        --server "$SERVER_ID" --name "$NAME" > "$WORK/profile.txt"
fi

echo "[2/5] Validating exact client share URI..."
docker compose exec -T admin python -m app.vless_e2e_probe_config     --stdin-url --socks-port "$PORT" < "$WORK/profile.txt" > "$WORK/client.json"
python3 -m json.tool "$WORK/client.json" >/dev/null

echo "[3/5] Checking public VPN endpoint TCP reachability from control plane..."
if ! python3 - "$WORK/client.json" <<'PY'
import json, socket, sys
cfg=json.load(open(sys.argv[1]))
remote=cfg["outbounds"][0]["settings"]["vnext"][0]
host=str(remote["address"])
port=int(remote["port"])
try:
    with socket.create_connection((host, port), timeout=7):
        pass
except OSError as exc:
    print(
        f"FAIL: public VPN endpoint {host}:{port} is unreachable from control plane "
        f"({type(exc).__name__}). Check provider firewall/security-group, UFW and routing."
    )
    raise SystemExit(1)
print(f"PASS: public VPN endpoint {host}:{port} accepts TCP")
PY
then
    exit 1
fi

XRAY_IMAGE="${XRAY_IMAGE:-ghcr.io/xtls/xray-core:26.6.27@sha256:c98160906f3fe1d9d16e950c2a7997dc0c542c71816aa92c21e510412f90e1a2}"
echo "[4/5] Starting clean Xray compatibility client ($XRAY_IMAGE)..."
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

echo "[5/5] Requesting multiple HTTPS destinations through VLESS Reality credentials..."
SUCCESS=0
probe() {
    local label="$1" url="$2"
    local code rc
    : > "$WORK/egress.txt"
    : > "$WORK/curl.log"
    code="$(curl -q -4 -sS --noproxy '' \
        --proxy "socks5h://127.0.0.1:$PORT" \
        --connect-timeout 8 --max-time 20 \
        -o "$WORK/egress.txt" -w '%{http_code}' "$url" 2>"$WORK/curl.log")" \
        && rc=0 || rc=$?
    if [[ "$rc" -eq 0 && "$code" =~ ^[1-5][0-9][0-9]$ ]]; then
        echo "PASS: $label (HTTP $code)"
        SUCCESS=$((SUCCESS + 1))
        if [[ "$label" == "ipify" && "$code" == "200" ]]; then
            python3 - "$WORK/egress.txt" <<'PY' || true
import ipaddress, pathlib, sys
try:
    value=pathlib.Path(sys.argv[1]).read_text().strip()
    ip=ipaddress.ip_address(value)
    if ip.is_global:
        print("VPN egress IPv4:", ip)
except Exception:
    pass
PY
        fi
    else
        echo "FAIL: $label (curl_exit=$rc http_status=${code:-none})"
        tail -n 3 "$WORK/curl.log" || true
    fi
}
probe "ipify" "https://api.ipify.org"
probe "cloudflare" "https://www.cloudflare.com/cdn-cgi/trace"
probe "google" "https://www.google.com/generate_204"

if (( SUCCESS < 2 )); then
    echo "FAIL: TCP endpoint is reachable but fewer than 2/3 HTTPS destinations worked through VLESS Reality."
    echo "This isolates the failure to REALITY/VLESS credentials, target/SNI, live Xray user state, or node outbound routing."
    docker logs --tail 40 "$CONTAINER" 2>&1 | \
        sed -E \
          -e 's/[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/[UUID]/g' \
          -e 's/[A-Za-z0-9_-]{43,100}/[REDACTED_LONG_STRING]/g'
    exit 1
fi
echo "PASS: VLESS Reality tunnel has internet egress ($SUCCESS/3 HTTPS destinations)."

if [[ "$HOT_ADD" == 1 ]]; then
    echo "PASS: fresh user hot-add works end-to-end WITHOUT Xray restart."
else
    echo "PASS: synthetic VPN probe user is retained for future read-only E2E checks."
    echo "PASS: dedicated VPN node works end-to-end without per-check credential churn"
fi
if [[ "$MODE" == "--publish" ]]; then
    echo "[publish] Final read-only integrity check and customer publication..."
    docker compose exec -T admin python -m app.promote_vpn_node --server "$SERVER_ID"
fi
