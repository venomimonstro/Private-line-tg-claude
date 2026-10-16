#!/usr/bin/env bash
# Full encrypted hop: local Xray -> trusted HTTPS -> Nginx/WebSocket relay
# -> direct REALITY node -> internet. Credential files never reach logs.
set -Eeuo pipefail
umask 077
cd "$(dirname "$0")/.."
XRAY_IMAGE="ghcr.io/xtls/xray-core:26.6.27@sha256:c98160906f3fe1d9d16e950c2a7997dc0c542c71816aa92c21e510412f90e1a2"
WORK="$(mktemp -d -t private-line-fallback-XXXXXXXX)"
NAME="private-line-fallback-probe-$(python3 -c 'import secrets; print(secrets.token_hex(5))')"
cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
PORT="$(python3 - <<'PY'
import socket
with socket.socket() as s:
    s.bind(('127.0.0.1', 0))
    print(s.getsockname()[1])
PY
)"
docker compose exec -T admin python -m app.https_fallback_probe_config --socks-port "$PORT" >"$WORK/client.json"
EXPECTED_IP="$(docker compose exec -T admin python -m app.https_fallback_probe_config --node-ip)"
python3 -m json.tool "$WORK/client.json" >/dev/null
docker run -d --name "$NAME" --network host --user 0:0 --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  -v "$WORK/client.json:/tmp/client.json:ro" "$XRAY_IMAGE" \
  run -c /tmp/client.json >/dev/null
READY=false
for _ in $(seq 1 20); do
  if python3 - "$PORT" <<'PY' >/dev/null 2>&1
import socket,sys
with socket.create_connection(('127.0.0.1',int(sys.argv[1])), timeout=1) as s:
    s.sendall(b'\x05\x01\x00')
    assert s.recv(2)==b'\x05\x00'
PY
  then READY=true; break; fi
  sleep 1
done
[[ "$READY" == true ]] || { echo "FAIL: fallback test SOCKS did not start" >&2; exit 1; }
EXIT_IP="$(curl -q -4 -fsS --noproxy '' --proxy "socks5h://127.0.0.1:$PORT" \
    --connect-timeout 8 --max-time 22 https://api.ipify.org)" || {
  echo "FAIL: HTTPS fallback cannot reach external IPv4 through node" >&2
  docker logs --tail 12 "$NAME" 2>&1 | sed -E 's/[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/[UUID]/g' >&2 || true
  exit 1
}
[[ "$EXIT_IP" == "$EXPECTED_IP" ]] || {
  echo "FAIL: fallback egress is not the dedicated VPN node" >&2; exit 1;
}
CODE="$(curl -q -4 -sS -o /dev/null -w '%{http_code}' --noproxy '' \
    --proxy "socks5h://127.0.0.1:$PORT" --connect-timeout 8 --max-time 22 \
    https://www.cloudflare.com/cdn-cgi/trace)" || {
  echo "FAIL: fallback cannot reach Cloudflare HTTPS" >&2; exit 1;
}
[[ "$CODE" =~ ^[1-5][0-9][0-9]$ ]] || { echo "FAIL: fallback HTTPS did not respond" >&2; exit 1; }
echo "PASS: trusted HTTPS/WebSocket relay -> dedicated node -> internet; node egress verified ($EXPECTED_IP)"
