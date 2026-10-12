#!/usr/bin/env bash
# Run on an existing standalone Marzban node, including over ssh 'bash -s'.
# Pin only the Xray executable to the broad-client compatibility baseline.
# Marzban image/database/users and all REALITY credentials are preserved.
set -Eeuo pipefail
umask 077

[ "$(id -u)" -eq 0 ] || { echo 'Run as root' >&2; exit 1; }
cd /opt/marzban
[ -f .env ] && [ -f docker-compose.yml ] || { echo 'Marzban installation not found'; exit 1; }

exec 9>/var/lock/private-line-xray-update.lock
flock -n 9 || { echo 'Another Xray update is running'; exit 1; }

VERSION=26.6.27
# GitHub Container Registry tagged-package digest for v26.6.27.
XRAY_IMAGE="ghcr.io/xtls/xray-core:26.6.27@sha256:c98160906f3fe1d9d16e950c2a7997dc0c542c71816aa92c21e510412f90e1a2"
CORE_DIR="/var/lib/marzban/xray-core/$VERSION"
CORE="$CORE_DIR/xray"
# Inspect the installed executable before pulling another copy. It is important
# on 512 MiB nodes that a temporary version-probe failure does not trigger a
# needless Docker pull and Marzban restart.
version_matches() {
    local binary="$1" output="" rc=0 first=""
    local attempt
    for attempt in 1 2 3; do
        rc=0
        output="$(timeout 20 "$binary" version 2>&1)" || rc=$?
        first="${output%%$'\n'*}"
        if [[ "$rc" -eq 0 && "$first" =~ ^Xray[[:space:]]+([0-9]+\.[0-9]+\.[0-9]+)([[:space:]]|$) ]]; then
            if [[ "${BASH_REMATCH[1]}" == "$VERSION" ]]; then
                return 0
            fi
            echo "FAIL: expected Xray $VERSION but executable reports: $first" >&2
            return 1
        fi
        if [[ "$attempt" -lt 3 ]]; then sleep 1; fi
    done
    echo "FAIL: unable to verify Xray $VERSION (exit=$rc; first line: ${first:0:160})" >&2
    return 1
}

# Use the container's /proc namespace for process verification. The host may
# resolve a container executable to an overlayfs path, not $CORE.
CURRENT_EXEC="$(grep -E '^[[:space:]]*(export[[:space:]]+)?XRAY_EXECUTABLE_PATH=' .env 2>/dev/null | tail -n1 | sed -E 's/^[^=]+=//' | tr -d '\r' || true)"
CURRENT_EXEC="${CURRENT_EXEC%\"}"
CURRENT_EXEC="${CURRENT_EXEC#\"}"
CURRENT_EXEC="${CURRENT_EXEC%\'}"
CURRENT_EXEC="${CURRENT_EXEC#\'}"
if [[ "$CURRENT_EXEC" == "$CORE" && -x "$CORE" ]]; then
    if version_matches "$CORE" && docker compose exec -T marzban python - "$CORE" <<'PY'
import base64, json, os, pathlib, re, socket, subprocess, sys, urllib.request

expected = sys.argv[1]
config_path = pathlib.Path("/var/lib/marzban/xray_config.json")
if not config_path.is_file():
    raise SystemExit(1)
cfg = json.loads(config_path.read_text())
rows = []
for inbound in cfg.get("inbounds", []):
    stream = inbound.get("streamSettings", {})
    if inbound.get("protocol") == "vless" and stream.get("security") == "reality":
        rows.append((inbound, stream.get("realitySettings", {})))
if len(rows) != 1:
    raise SystemExit(1)
inbound, reality = rows[0]
stream = inbound.get("streamSettings", {})
if inbound.get("port") != 443:
    raise SystemExit(1)
if stream.get("network") not in ("tcp", "raw"):
    raise SystemExit(1)
if inbound.get("settings", {}).get("decryption") != "none":
    raise SystemExit(1)
private_key = reality.get("privateKey")
public_key = reality.get("publicKey")
short_ids = reality.get("shortIds")
server_names = reality.get("serverNames")
if (
    not isinstance(private_key, str)
    or not private_key
    or not isinstance(public_key, str)
    or not re.fullmatch(r"[A-Za-z0-9_-]{43,44}", public_key)
    or not isinstance(short_ids, list)
    or not any(isinstance(x, str) and re.fullmatch(r"(?:[0-9a-f]{2}){1,8}", x) for x in short_ids)
    or not isinstance(server_names, list)
    or len(server_names) != 1
):
    # The static config may still be accepted by Xray while lacking metadata
    # required by Marzban 0.8.x. Fall through to the repair path.
    raise SystemExit(1)

result = subprocess.run(
    [expected, "x25519", "-i", private_key],
    capture_output=True, text=True, timeout=10,
)
if result.returncode:
    raise SystemExit(1)
derived = None
for line in result.stdout.splitlines():
    label, sep, value = line.partition(":")
    label = re.sub(r"\s+", "", label).lower()
    if sep and label in ("publickey", "password", "password(publickey)"):
        derived = value.strip()
        break
if derived != public_key:
    raise SystemExit(1)

running = False
for item in pathlib.Path("/proc").iterdir():
    if not item.name.isdigit():
        continue
    try:
        if os.readlink(item / "exe") == expected:
            running = True
            break
    except OSError:
        continue
if not running:
    raise SystemExit(1)
with socket.create_connection(("127.0.0.1", 443), timeout=3):
    pass
with urllib.request.urlopen("http://127.0.0.1:8000/openapi.json", timeout=4) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
    then
        if docker compose exec -T marzban "$CORE" run -test -config /var/lib/marzban/xray_config.json </dev/null >/dev/null 2>&1; then
            echo "[1/4] PASS: verified installed Xray $VERSION; no pull required"
            echo "[2/4] PASS: canonical REALITY metadata and keypair verified"
            echo "[3/4] PASS: live Marzban process and API are ready"
            echo "[4/4] PASS: Xray :443; no restart required"
            exit 0
        fi
    fi
fi

WORKDIR="$(mktemp -d /var/tmp/private-line-xray-XXXXXXXX)"
BACKUP_DIR="/var/lib/marzban/backups/core-switch-$(date -u +%Y%m%dT%H%M%SZ)-$$"
CHANGED=false
SUCCESS=false
TMP_CONTAINER=""

cleanup() {
    result=$?
    trap - EXIT
    if [ -n "$TMP_CONTAINER" ]; then
        docker rm -f "$TMP_CONTAINER" >/dev/null 2>&1 || true
    fi
    if [ "$SUCCESS" != true ] && [ "$result" -eq 0 ]; then
        echo 'FAIL: core switch ended before startup verification' >&2
        result=1
    fi
    if [ "$CHANGED" = true ] && [ "$SUCCESS" != true ]; then
        echo 'Startup verification failed; restoring previous Xray configuration'
        cp -p "$BACKUP_DIR/marzban.env" /opt/marzban/.env
        cp -p "$BACKUP_DIR/xray_config.json" /var/lib/marzban/xray_config.json
        docker compose up -d --no-deps --force-recreate marzban </dev/null || true
    fi
    rm -rf -- "$WORKDIR"
    exit "$result"
}
trap cleanup EXIT

echo "[1/4] Pulling digest-pinned Xray compatibility core $VERSION"
docker pull "$XRAY_IMAGE" >/dev/null
TMP_CONTAINER="$(docker create "$XRAY_IMAGE")"
docker cp "$TMP_CONTAINER:/usr/local/bin/xray" "$WORKDIR/xray"
docker rm "$TMP_CONTAINER" >/dev/null
TMP_CONTAINER=""
chmod 755 "$WORKDIR/xray"
version_matches "$WORKDIR/xray" || {
    echo "FAIL: unexpected Xray binary version; active Marzban has not been changed" >&2
    exit 2
}
mkdir -p "$CORE_DIR"
install -m 755 "$WORKDIR/xray" "$CORE_DIR/xray.next"
mv -f "$CORE_DIR/xray.next" "$CORE"

echo '[2/4] Validating preserved REALITY configuration'
mkdir -p "$BACKUP_DIR"
cp -p .env "$BACKUP_DIR/marzban.env"
cp -p /var/lib/marzban/xray_config.json "$BACKUP_DIR/xray_config.json"
chmod 600 "$BACKUP_DIR/marzban.env" "$BACKUP_DIR/xray_config.json"
CANDIDATE="$BACKUP_DIR/xray_config.candidate.json"

docker compose exec -T marzban python - "$CORE" "$CANDIDATE" <<'PY'
import base64, json, os, pathlib, re, subprocess, sys

source = pathlib.Path('/var/lib/marzban/xray_config.json')
config = json.loads(source.read_text())

def has_finalmask(value):
    if isinstance(value, dict):
        return any(
            str(key).lower() == 'finalmask' or has_finalmask(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(has_finalmask(item) for item in value)
    return False

if has_finalmask(config):
    raise SystemExit(
        'FAIL: finalmask is forbidden on the compatibility REALITY profile; no changes applied'
    )

count = 0
for inbound in config.get('inbounds', []):
    stream = inbound.get('streamSettings', {})
    if stream.get('security') != 'reality':
        continue
    reality = stream.get('realitySettings', {})
    private_key = reality.get('privateKey')
    if not isinstance(private_key, str) or not private_key:
        raise SystemExit('FAIL: Reality private key missing; no changes applied')
    short_ids = reality.get('shortIds')
    server_names = reality.get('serverNames')
    if not isinstance(short_ids, list) or not any(short_ids):
        raise SystemExit('FAIL: Reality shortIds missing; no changes applied')
    if not isinstance(server_names, list) or not any(server_names):
        raise SystemExit('FAIL: Reality serverNames missing; no changes applied')

    result = subprocess.run(
        [sys.argv[1], 'x25519', '-i', private_key],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode:
        raise SystemExit('FAIL: existing Reality private key cannot be decoded')
    public_key = None
    for line in result.stdout.splitlines():
        label, sep, value = line.partition(':')
        label = re.sub(r'\s+', '', label).lower()
        if sep and label in ('publickey', 'password', 'password(publickey)'):
            public_key = value.strip()
            break
    if (
        not public_key
        or not re.fullmatch(r'[A-Za-z0-9_-]{43,44}', public_key)
        or len(base64.urlsafe_b64decode(public_key + '=' * (-len(public_key) % 4))) != 32
    ):
        raise SystemExit('FAIL: unable to derive Reality public key; no changes applied')

    # Repair compatibility metadata without rotating any credential.
    # Marzban 0.8.x can derive a REALITY public key only from older xray x25519
    # output labels, while Xray 26.x prints Password (PublicKey). Persisting the
    # derived value avoids that cross-version parser dependency.
    reality['publicKey'] = public_key
    reality['show'] = False
    reality['minClientVer'] = ""
    reality['maxClientVer'] = ""
    reality['maxTimeDiff'] = 0
    inbound.setdefault('settings', {})['decryption'] = 'none'
    stream = inbound.setdefault('streamSettings', {})
    if stream.get('network') not in ('tcp', 'raw'):
        raise SystemExit('FAIL: VLESS Reality transport must be TCP/RAW')
    count += 1

if count != 1:
    raise SystemExit(f'FAIL: expected exactly one Reality inbound, got {count}')

candidate = pathlib.Path(sys.argv[2])
with open(candidate, 'x') as stream:
    json.dump(config, stream, indent=2)
    stream.write('\n')
os.chmod(candidate, 0o600)
print('PASS: preserved one Reality inbound; credentials unchanged')
PY

if ! docker compose exec -T marzban "$CORE" run -test     -config "$CANDIDATE" </dev/null > "$WORKDIR/validation.log" 2>&1; then
    echo 'FAIL: compatibility core rejected the preserved config; active runtime unchanged'
    tail -n 20 "$WORKDIR/validation.log" | sed -E 's/[A-Za-z0-9_=-]{32,}/[REDACTED]/g'
    exit 1
fi

CHANGED=true
install -m 600 "$CANDIDATE" /var/lib/marzban/xray_config.json
python3 - "$CORE" <<'PY'
import os, pathlib, re, sys
path = pathlib.Path('/opt/marzban/.env')
lines = [
    line for line in path.read_text().splitlines()
    if not re.match(r'^\s*(?:export\s+)?XRAY_EXECUTABLE_PATH\s*=', line)
]
lines.append('XRAY_EXECUTABLE_PATH=' + sys.argv[1])
tmp = path.with_name('.env.xray-switch')
with open(tmp, 'w') as stream:
    stream.write('\n'.join(lines) + '\n')
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY

echo '[3/4] Restarting Marzban with compatibility core'
docker compose up -d --no-deps --force-recreate marzban </dev/null
for _ in $(seq 1 40); do
    if docker compose exec -T marzban python - "$CORE" <<'PY'
import os, pathlib, socket, sys, urllib.request
expected = sys.argv[1]
running = False
for item in pathlib.Path('/proc').iterdir():
    if not item.name.isdigit():
        continue
    try:
        if os.readlink(item / 'exe') == expected:
            running = True
            break
    except OSError:
        pass
if not running:
    raise SystemExit(1)
with socket.create_connection(('127.0.0.1', 443), timeout=2):
    pass
with urllib.request.urlopen('http://127.0.0.1:8000/openapi.json', timeout=2) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
    then
        SUCCESS=true
        break
    fi
    sleep 2
done

if [ "$SUCCESS" != true ]; then
    echo 'FAIL: compatibility core did not become ready. Recent Marzban errors:'
    docker compose logs --no-color --tail 100 marzban </dev/null 2>&1 |         sed -E           -e 's/[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/[UUID]/g'           -e 's/[A-Za-z0-9_=-]{32,}/[REDACTED]/g' | tail -n 40 || true
    exit 1
fi

echo "[4/4] PASS: Xray $VERSION is running on :443; Marzban API is ready"
echo "Rollback backup: $BACKUP_DIR"
echo 'Preserved: Marzban DB, client UUIDs, Reality private key, short IDs, SNI.'
echo 'NEXT on MAIN: reconcile runtime, then run paid-key and disposable-user E2E probes.'
