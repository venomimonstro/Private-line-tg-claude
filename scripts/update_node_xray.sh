#!/usr/bin/env bash
# Run on an existing standalone Marzban node, including over ssh 'bash -s'.
# Upgrade only Xray. Keep Marzban image, database, users and Reality config.
set -euo pipefail
umask 077
[ "$(id -u)" -eq 0 ] || { echo 'Run as root' >&2; exit 1; }
cd /opt/marzban
[ -f .env ] && [ -f docker-compose.yml ] || { echo 'Marzban installation not found'; exit 1; }
exec 9>/var/lock/private-line-xray-update.lock
flock -n 9 || { echo 'Another Xray update is running'; exit 1; }
VERSION=26.9.9
case "$(uname -m)" in
    x86_64) ASSET=Xray-linux-64.zip; SHA256=1eb9175d0f0a8f8149c9230a7fc5ae66ce332ed20a53155ce61fe62e3f58b7df ;;
    aarch64|arm64) ASSET=Xray-linux-arm64-v8a.zip; SHA256=3e38d72dfc5eb65c91df0e5583e9b6676c32232041da47de6ae73946b526d66c ;;
    *) echo 'Unsupported architecture'; exit 1 ;;
esac
CORE_DIR="/var/lib/marzban/xray-core/$VERSION"
CORE="$CORE_DIR/xray"
WORKDIR="$(mktemp -d /var/tmp/private-line-xray-XXXXXXXX)"
BACKUP_DIR="/var/lib/marzban/backups/core-update-$(date -u +%Y%m%dT%H%M%SZ)-$$"
CHANGED=false
SUCCESS=false
cleanup() {
    result=$?
    trap - EXIT
    if [ "$SUCCESS" != true ] && [ "$result" -eq 0 ]; then
        echo 'FAIL: update ended before startup verification' >&2
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

echo "[1/4] Downloading official Xray $VERSION and verifying SHA-256"
curl -fL --retry 3 --connect-timeout 15 --max-time 180 \
    "https://github.com/XTLS/Xray-core/releases/download/v$VERSION/$ASSET" -o "$WORKDIR/core.zip"
printf '%s  %s\n' "$SHA256" "$WORKDIR/core.zip" | sha256sum --check --status
python3 - "$WORKDIR/core.zip" "$WORKDIR/xray" <<'PY'
import pathlib, sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as archive:
    pathlib.Path(sys.argv[2]).write_bytes(archive.read('xray'))
PY
chmod 755 "$WORKDIR/xray"
"$WORKDIR/xray" version | head -n 1
mkdir -p "$CORE_DIR"
# Atomic install; a running process keeps its existing executable inode.
install -m 755 "$WORKDIR/xray" "$CORE_DIR/xray.next"
mv -f "$CORE_DIR/xray.next" "$CORE"

echo '[2/4] Preparing Marzban-compatible Reality metadata'
mkdir -p "$BACKUP_DIR"
cp -p .env "$BACKUP_DIR/marzban.env"
cp -p /var/lib/marzban/xray_config.json "$BACKUP_DIR/xray_config.json"
chmod 600 "$BACKUP_DIR/marzban.env" "$BACKUP_DIR/xray_config.json"
CANDIDATE="$BACKUP_DIR/xray_config.candidate.json"
# Old Marzban parses "Public key:" from x25519 output. New Xray emits
# "Password:". Supplying the derived publicKey avoids that obsolete parser.
# No new private keys, short IDs or client UUIDs are generated.
docker compose exec -T marzban python - "$CORE" "$CANDIDATE" <<'PY'
import base64, json, os, pathlib, re, subprocess, sys
source = pathlib.Path('/var/lib/marzban/xray_config.json')
config = json.loads(source.read_text())
count = 0
for inbound in config.get('inbounds', []):
    stream = inbound.get('streamSettings', {})
    if stream.get('security') != 'reality':
        continue
    reality = stream.get('realitySettings', {})
    private_key = reality.get('privateKey')
    if not isinstance(private_key, str) or not private_key:
        raise SystemExit('FAIL: Reality private key missing; no changes applied')
    result = subprocess.run(
        [sys.argv[1], 'x25519', '-i', private_key],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode:
        raise SystemExit('FAIL: existing Reality key cannot be decoded')
    public_key = None
    for line in result.stdout.splitlines():
        label, sep, value = line.partition(':')
        label = re.sub(r'\s+', '', label).lower()
        if sep and label in ('publickey', 'password', 'password(publickey)'):
            public_key = value.strip()
            break
    if (not public_key or not re.fullmatch(r'[A-Za-z0-9_-]{43,44}', public_key)
        or len(base64.urlsafe_b64decode(public_key + '=' * (-len(public_key) % 4))) != 32):
        raise SystemExit('FAIL: unable to derive Reality public key; no changes applied')
    reality['publicKey'] = public_key
    count += 1
if not count:
    raise SystemExit('FAIL: no Reality inbound found')
candidate = pathlib.Path(sys.argv[2])
with open(candidate, 'x') as stream:
    json.dump(config, stream, indent=2)
    stream.write('\n')
os.chmod(candidate, 0o600)
print('Derived publicKey for %d Reality inbound(s); original private keys preserved' % count)
PY
# -T disables TTY, NOT stdin. Never let Docker consume an SSH bash -s script.
if ! docker compose exec -T marzban "$CORE" run -test \
    -config "$CANDIDATE" </dev/null > "$WORKDIR/validation.log" 2>&1; then
    echo 'FAIL: new core rejected the candidate; active configuration unchanged'
    exit 1
fi
# Backups exist before either live file changes; rollback restores both files.
CHANGED=true
install -m 600 "$CANDIDATE" /var/lib/marzban/xray_config.json
python3 - "$CORE" <<'PY'
import os, pathlib, re, sys
path = pathlib.Path('/opt/marzban/.env')
lines = [line for line in path.read_text().splitlines()
         if not re.match(r'^\s*(?:export\s+)?XRAY_EXECUTABLE_PATH\s*=', line)]
lines.append('XRAY_EXECUTABLE_PATH=' + sys.argv[1])
tmp = path.with_name('.env.xray-update')
with open(tmp, 'w') as stream:
    stream.write('\n'.join(lines) + '\n')
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY

echo '[3/4] Restarting Marzban with the pinned Xray core'
docker compose up -d --no-deps --force-recreate marzban </dev/null
for attempt in $(seq 1 30); do
    if docker compose exec -T marzban python - "$CORE" <<'PY'
import os, pathlib, socket, sys, urllib.request
expected = sys.argv[1]
# Check the executable actually running, rather than the old binary on PATH.
found = False
for item in pathlib.Path('/proc').iterdir():
    if item.name.isdigit():
        try:
            if os.readlink(item / 'exe') == expected:
                found = True
                break
        except OSError:
            pass
if not found:
    raise SystemExit(1)
try:
    with socket.create_connection(('127.0.0.1', 443), timeout=2):
        pass
    with urllib.request.urlopen('http://127.0.0.1:8000/openapi.json', timeout=2) as response:
        if response.status != 200:
            raise SystemExit(1)
except OSError:
    raise SystemExit(1)
PY
    then
        SUCCESS=true
        break
    fi
    sleep 2
done
if [ "$SUCCESS" != true ]; then
    echo 'FAIL: new core did not become ready. Marzban startup errors (redacted):'
    docker compose logs --no-color --tail 80 marzban </dev/null 2>&1 | \
        sed -E -e 's/[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/[UUID]/g' \
               -e 's/[A-Za-z0-9_=-]{32,}/[REDACTED]/g' | tail -n 35 || true
    exit 1
fi
echo "[4/4] Core $VERSION is running; VPN port and Marzban API available"
echo "Rollback env backup: $BACKUP_DIR/marzban.env"
echo 'UUIDs, Reality private key, short IDs, SNI and database preserved.'
echo 'NEXT: on MAIN server run: cd /opt/private-line && bash scripts/probe_vless_e2e.sh 3'
echo 'Startup verified only. A real VPN probe must pass before declaring success.'
