#!/usr/bin/env bash
# Real VPN handshake/egress test from the MAIN server through a temporary
# Xray client to a registered VLESS REALITY node. Does not modify production.
# Usage: bash scripts/probe_vless_e2e.sh 2
set -euo pipefail
umask 077

SUB_ID="${1:-}"
SERVER_ID="${2:-}"
SERVER_ARGS=()
if [[ -n "$SERVER_ID" ]]; then
    [[ "$SERVER_ID" =~ ^[1-9][0-9]*$ ]] || { echo "Invalid server ID" >&2; exit 2; }
    SERVER_ARGS=(--server "$SERVER_ID")
fi
[[ "$SUB_ID" == "--telegram-url" || "$SUB_ID" == "--stdin-url" || "$SUB_ID" =~ ^[1-9][0-9]*$ ]] || {
    echo "Usage: bash scripts/probe_vless_e2e.sh SUBSCRIPTION_ID [SERVER_ID] | --stdin-url SERVER_ID | --telegram-url SERVER_ID" >&2
    exit 2
}
cd "$(dirname "$0")/.."

XRAY_IMAGE="${XRAY_IMAGE:-ghcr.io/xtls/xray-core:26.6.27@sha256:c98160906f3fe1d9d16e950c2a7997dc0c542c71816aa92c21e510412f90e1a2}"
SOCKS_PORT="$(python3 - <<'PY'
import socket
s=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
[[ "$SOCKS_PORT" =~ ^[1-9][0-9]{3,4}$ ]] || {
    echo "FAIL: could not allocate local paid-probe SOCKS port" >&2
    exit 2
}
RUN_ID="$(python3 -c 'import secrets; print(secrets.token_hex(6))')"
PROBE_NAME="private-line-vless-probe-${SERVER_ID:-any}-${RUN_ID}"
EXPECTED_NODE_IP=""
EXIT_IP=""
WORKDIR="$(mktemp -d -t private-line-vless-XXXXXXXX)"
CONFIG="$WORKDIR/xray-client.json"

cleanup() {
    docker rm -f "$PROBE_NAME" >/dev/null 2>&1 || true
    rm -f -- "$CONFIG" "$WORKDIR/egress.txt" "$WORKDIR/curl.log"
    rmdir "$WORKDIR" 2>/dev/null || true
}
trap cleanup EXIT

echo "[1/5] Checking local temporary SOCKS port..."
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

if [[ "$SUB_ID" == "--stdin-url" ]]; then
    [[ -n "$SERVER_ID" ]] || { echo "Server ID required" >&2; exit 2; }
    echo "[2/5] Preparing supplied direct-Xray canary profile..."
    docker compose exec -T admin python -m app.vless_e2e_probe_config         --stdin-url --socks-port "$SOCKS_PORT" > "$CONFIG" || exit 2
elif [[ "$SUB_ID" == "--telegram-url" ]]; then
    [[ -n "$SERVER_ID" ]] || { echo "Server ID required" >&2; exit 2; }
    [[ -r /dev/tty ]] || { echo "Interactive terminal required" >&2; exit 2; }
    VLESS_URL=""
    for attempt in 1 2 3; do
        printf 'Paste ONLY the full vless:// link from Telegram, then Enter (hidden input, attempt %s/3): ' "$attempt" > /dev/tty
        IFS= read -r -s VLESS_URL < /dev/tty || { echo "No input received" >&2; exit 2; }
        printf '\n' > /dev/tty
        if [[ -z "$VLESS_URL" ]]; then
            echo "No text was pasted. In the SSH terminal use its paste command (often Ctrl+Shift+V or right-click), then Enter." >&2
        elif [[ "$VLESS_URL" != vless://* ]]; then
            echo "Input does not start with vless://. Copy the key from /mykey, not the subscription URL or terminal output." >&2
        elif printf '%s\n' "$VLESS_URL" | python3 -c '
import sys
from urllib.parse import urlsplit
try:
    link = urlsplit(sys.stdin.readline().strip())
    valid = bool(link.username and link.hostname and link.port and link.query)
except ValueError:
    valid = False
sys.exit(0 if valid else 1)
'; then
            break
        else
            echo "Incomplete VLESS link. Copy the entire line from the bot message /mykey (including UUID, host, port and ?parameters)." >&2
        fi
        VLESS_URL=""
    done
    [[ -n "$VLESS_URL" ]] || { echo "No complete VLESS link received; nothing was changed." >&2; exit 2; }
    echo "[2/5] Comparing supplied Telegram key with node settings (read-only)..."
    # Run current host source with the existing application environment. No
    # image build, service restart, key repair or mutation is involved.
    printf '%s\n' "$VLESS_URL" | docker compose exec -T admin python -c \
        "$(cat app/check_exact_vless.py)" --server "$SERVER_ID" || exit 2
    printf '%s\n' "$VLESS_URL" | docker compose exec -T admin python -c \
        "$(cat app/vless_e2e_probe_config.py)" --stdin-url --socks-port "$SOCKS_PORT" > "$CONFIG" || exit 2
    unset VLESS_URL
else
echo "[2/5] Preparing confidential Xray config for subscription $SUB_ID..."
if ! docker compose exec -T admin python -m app.vless_e2e_probe_config \
    --subscription "$SUB_ID" "${SERVER_ARGS[@]}" --socks-port "$SOCKS_PORT" >"$CONFIG"; then
    echo "No active VLESS key found under subscription $SUB_ID." >&2
    if [[ -n "$SERVER_ID" ]]; then
        echo "ERROR: server-scoped acceptance is non-interactive and requires an existing key." >&2
        exit 2
    fi
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
fi
python3 -m json.tool "$CONFIG" >/dev/null

EXPECTED_NODE_IP="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["outbounds"][0]["settings"]["vnext"][0]["address"])' "$CONFIG")"
echo "[3/5] Checking public VPN endpoint TCP reachability..."
if ! python3 - "$CONFIG" <<'PY'
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
        f"FAIL: public VPN endpoint {host}:{port} is unreachable "
        f"({type(exc).__name__}); the VLESS key cannot work from this vantage point."
    )
    raise SystemExit(1)
print(f"PASS: public VPN endpoint {host}:{port} accepts TCP")
PY
then
    exit 1
fi

echo "[4/5] Starting ephemeral Xray client ($XRAY_IMAGE)..."
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
    if python3 -c 'import socket,sys; s=socket.create_connection(("127.0.0.1",int(sys.argv[1])),timeout=1); s.sendall(b"\x05\x01\x00"); assert s.recv(2)==b"\x05\x00"; s.close()' "$SOCKS_PORT" >/dev/null 2>&1; then
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

echo "[5/5] Testing multiple destinations through the SAME VLESS tunnel..."
# A single HTTP endpoint can be blocked or unavailable even when the VPN works.
# HTTPS + HTTP and IP + domain tests isolate egress / TLS / resolver failures.
SUCCESS=0
FAILURES=0
probe() {
    local label="$1" url="$2" body="$WORKDIR/egress.txt" err="$WORKDIR/curl.log"
    local code exit_code
    : > "$body"
    : > "$err"
    code="$(curl -q -4 -sS -o "$body" -w '%{http_code}' \
        --noproxy '' --proxy "socks5h://127.0.0.1:$SOCKS_PORT" \
        --connect-timeout 7 --max-time 16 \
        "$url" 2>"$err")" && exit_code=0 || exit_code=$?
    if [[ "$exit_code" == 0 && "$code" =~ ^[1-5][0-9][0-9]$ ]]; then
        echo "PASS: $label (HTTP $code)"
        SUCCESS=$((SUCCESS + 1))
        if [[ "$label" == "IPv4 via HTTPS" ]]; then
            EXIT_IP="$(head -c 64 "$body" | tr -dc '0-9.')"
            [[ "$EXIT_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] && \
                echo "VPN egress IPv4: $EXIT_IP"
        fi
    else
        echo "FAIL: $label (curl_exit=$exit_code http_status=${code:-none})"
        sed -E \
            -e 's/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/[UUID]/g' \
            "$err" | tail -n 3
        FAILURES=$((FAILURES + 1))
    fi
}
probe "IPv4 via HTTPS" "https://api.ipify.org"
probe "Cloudflare HTTPS" "https://www.cloudflare.com/cdn-cgi/trace"
probe "Google HTTPS" "https://www.google.com/generate_204"
probe "Cloudflare HTTP (IP)" "http://1.1.1.1/cdn-cgi/trace"
probe "Google HTTP" "http://connectivitycheck.gstatic.com/generate_204"

echo "RESULT: $SUCCESS/5 destinations returned HTTP responses through the VPN."
if [[ "$SUB_ID" == "--telegram-url" ]]; then
    # Windows sing-box clients may use Google DNS-over-TLS. HTTPS egress alone
    # does not prove that their resolver can connect on TCP/853.
    python3 - "$SOCKS_PORT" <<'PY'
import socket, ssl, struct, sys
def read_exact(stream, count):
    data = b""
    while len(data) < count:
        chunk = stream.recv(count - len(data))
        if not chunk:
            raise OSError("Connection closed during DNS probe")
        data += chunk
    return data
try:
    with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=7) as sock:
        sock.settimeout(8)
        sock.sendall(b"\x05\x01\x00")
        if read_exact(sock, 2) != b"\x05\x00":
            raise OSError("SOCKS negotiation failed")
        sock.sendall(b"\x05\x01\x00\x01" + socket.inet_aton("8.8.8.8") + struct.pack("!H", 853))
        reply = read_exact(sock, 4)
        if len(reply) != 4 or reply[1] != 0:
            raise OSError("TCP/853 via tunnel refused")
        address_len = {1: 4, 4: 16}.get(reply[3])
        if reply[3] == 3:
            address_len = read_exact(sock, 1)[0]
        if address_len is None:
            raise OSError("Unexpected SOCKS address")
        read_exact(sock, address_len + 2)
        with ssl.create_default_context().wrap_socket(sock, server_hostname="dns.google") as tls:
            # Standard DNS-over-TLS wire query for example.com A.
            query = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
            tls.sendall(struct.pack("!H", len(query)) + query)
            length = read_exact(tls, 2)
            response = read_exact(tls, struct.unpack("!H", length)[0])
            if len(response) < 12 or response[:2] != b"\x12\x34" or response[3] & 15:
                raise OSError("DNS-over-TLS response invalid")
    print("PASS: Google DNS-over-TLS 8.8.8.8:853 via the paid VLESS tunnel")
except (OSError, ssl.SSLError, ValueError) as exc:
    print("WARN: Google DNS-over-TLS 8.8.8.8:853 via tunnel failed (%s)" % type(exc).__name__)
PY
    dot_code="$(curl -q -4 -sS -o "$WORKDIR/egress.txt" -w '%{http_code}' \
        --noproxy '' --proxy "socks5h://127.0.0.1:$SOCKS_PORT" \
        -H 'Accept: application/dns-json' --connect-timeout 7 --max-time 16 \
        'https://1.1.1.1/dns-query?name=example.com&type=A' 2>"$WORKDIR/curl.log" || true)"
    if [[ "$dot_code" == "200" ]]; then
        echo "PASS: Cloudflare DNS-over-HTTPS 1.1.1.1:443 via the paid VLESS tunnel"
    else
        echo "WARN: Cloudflare DNS-over-HTTPS 1.1.1.1:443 via tunnel failed (HTTP ${dot_code:-none})"
    fi
fi
if [[ "$SUB_ID" == "--telegram-url" && "$EXIT_IP" != "$EXPECTED_NODE_IP" ]]; then
    echo "FAIL: exact-key test could not verify node egress IP (expected $EXPECTED_NODE_IP)."
    exit 1
fi
if (( SUCCESS >= 3 )); then
    echo "PASS: tunnel has functional internet egress on multiple destinations."
    exit 0
fi
echo "FAIL: endpoint TCP was reachable but the VLESS/REALITY tunnel did not provide sufficient internet egress."
echo "Inspect REALITY target/SNI, UUID+flow assignment, live Xray DB-user state, node DNS/outbound routing."
echo "Temporary Xray client logs:"
docker logs --tail 25 "$PROBE_NAME" 2>&1 | \
    sed -E 's/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/[UUID]/g'
echo "NEXT: on MAIN server run: ssh root@185.236.25.152 'bash -s' < scripts/inspect_node_vless.sh"
exit 1
