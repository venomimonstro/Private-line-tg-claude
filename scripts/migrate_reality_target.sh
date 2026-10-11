#!/usr/bin/env bash
# Migrate an existing Marzban VLESS REALITY node to a safer target without
# rotating UUIDs, Reality private key or short IDs.
set -Eeuo pipefail
umask 077

TARGET_HOST="${1:-dl.google.com}"
case "$TARGET_HOST" in
  *[!A-Za-z0-9.-]*|"") echo "FAIL: invalid target hostname" >&2; exit 2 ;;
esac

[ "$(id -u)" -eq 0 ] || { echo "Run as root" >&2; exit 2; }
cd /opt/marzban
[ -f .env ] && [ -f docker-compose.yml ] || {
  echo "FAIL: /opt/marzban installation not found" >&2
  exit 2
}
CONFIG=/var/lib/marzban/xray_config.json
[ -f "$CONFIG" ] || { echo "FAIL: $CONFIG missing" >&2; exit 2; }

echo "[1/5] Validating candidate REALITY target $TARGET_HOST..."
TLS_LOG="$(mktemp)"
if ! timeout 15 openssl s_client -connect "$TARGET_HOST:443"     -servername "$TARGET_HOST" -tls1_3 -alpn h2 </dev/null     >"$TLS_LOG" 2>&1; then
  rm -f "$TLS_LOG"
  echo "FAIL: candidate target TLS1.3 handshake failed" >&2
  exit 2
fi
if ! grep -q 'ALPN protocol: h2' "$TLS_LOG"; then
  rm -f "$TLS_LOG"
  echo "FAIL: candidate target did not negotiate HTTP/2" >&2
  exit 2
fi
rm -f "$TLS_LOG"

BACKUP="/var/lib/marzban/backups/reality-target-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$BACKUP"
cp -p "$CONFIG" "$BACKUP/xray_config.json"
chmod 600 "$BACKUP/xray_config.json"
CANDIDATE="$BACKUP/xray_config.candidate.json"

rollback() {
  rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ]; then
    echo "Migration failed; restoring previous Xray config..." >&2
    cp -p "$BACKUP/xray_config.json" "$CONFIG"
    docker compose up -d --no-deps --force-recreate marzban </dev/null || true
  fi
  exit "$rc"
}
trap rollback EXIT

echo "[2/5] Building candidate while preserving credentials..."
TARGET_HOST="$TARGET_HOST" CONFIG="$CONFIG" CANDIDATE="$CANDIDATE" python3 - <<'PY'
import json, os, pathlib, re, stat
src = pathlib.Path(os.environ["CONFIG"])
dst = pathlib.Path(os.environ["CANDIDATE"])
target = os.environ["TARGET_HOST"]
cfg = json.loads(src.read_text())
changed = 0
for inbound in cfg.get("inbounds", []):
    stream = inbound.get("streamSettings", {})
    if inbound.get("protocol") != "vless" or stream.get("security") != "reality":
        continue
    reality = stream.get("realitySettings", {})
    if not reality.get("privateKey"):
        raise SystemExit("FAIL: Reality privateKey missing")
    short_ids = reality.get("shortIds")
    if not isinstance(short_ids, list) or not any(isinstance(x, str) and x for x in short_ids):
        raise SystemExit("FAIL: Reality shortIds missing")
    if "target" in reality:
        reality["target"] = target + ":443"
    else:
        reality["dest"] = target + ":443"
    reality["serverNames"] = [target]
    changed += 1
if changed != 1:
    raise SystemExit(f"FAIL: expected exactly one VLESS Reality inbound, found {changed}")
dst.write_text(json.dumps(cfg, indent=2) + "\n")
os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR)
print("Candidate preserves privateKey/shortIds and changes target/SNI only")
PY

echo "[3/5] Validating candidate with the installed Xray core..."
docker compose exec -T marzban sh -lc '
  core="${XRAY_EXECUTABLE_PATH:-/usr/local/bin/xray}"
  "$core" run -test -config "'"$CANDIDATE"'"
' </dev/null >/dev/null

echo "[4/5] Applying target and rebuilding Marzban/Xray runtime..."
install -m 600 "$CANDIDATE" "$CONFIG"
docker compose up -d --no-deps --force-recreate marzban </dev/null

READY=false
for _ in $(seq 1 30); do
  if curl -q -fsS --max-time 3 http://127.0.0.1:8000/openapi.json >/dev/null 2>&1      && ss -lnt | grep -qE '[:.]443[[:space:]]'; then
    READY=true
    break
  fi
  sleep 2
done
[ "$READY" = true ] || { echo "FAIL: Marzban/Xray did not recover" >&2; exit 2; }

echo "[5/5] Static configuration check..."
python3 - "$CONFIG" "$TARGET_HOST" <<'PY'
import json, sys
cfg=json.load(open(sys.argv[1]))
target=sys.argv[2]
rows=[]
for i in cfg.get("inbounds", []):
    r=i.get("streamSettings",{}).get("realitySettings",{})
    if i.get("protocol")=="vless" and r:
        rows.append(r)
assert len(rows)==1
r=rows[0]
assert (r.get("target") or r.get("dest")) == target + ":443"
assert r.get("serverNames") == [target]
assert r.get("privateKey")
assert any(r.get("shortIds") or [])
print("PASS: REALITY target migrated; credentials preserved")
PY

trap - EXIT
echo "Backup: $BACKUP/xray_config.json"
echo "NEXT on MAIN: reconcile Marzban hosts/client URLs, then run disposable E2E smoke."
