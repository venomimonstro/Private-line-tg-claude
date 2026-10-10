#!/usr/bin/env bash
# Safely migrate an existing Marzban env-based sudo admin into the Marzban DB.
# Preserves the same username/password, Xray config, users, UUIDs and Reality keys.
set -Eeuo pipefail
umask 077

[ "$(id -u)" -eq 0 ] || { echo "Run as root" >&2; exit 2; }
cd /opt/marzban
[ -f .env ] && [ -f docker-compose.yml ] || {
  echo "FAIL: /opt/marzban installation not found" >&2
  exit 2
}

BACKUP="/opt/marzban/.env.before-db-admin-$(date -u +%Y%m%dT%H%M%SZ)"
cp -p .env "$BACKUP"

rollback() {
  rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ]; then
    echo "Migration failed; restoring original Marzban .env" >&2
    cp -p "$BACKUP" .env
    docker compose up -d --no-deps --force-recreate marzban </dev/null || true
  fi
  exit "$rc"
}
trap rollback EXIT

if ! grep -qE '^[[:space:]]*(export[[:space:]]+)?SUDO_USERNAME[[:space:]]*=' .env 2>/dev/null; then
  echo "PASS: Marzban no longer uses env-based sudo admin."
  rm -f "$BACKUP"
  trap - EXIT
  exit 0
fi
if ! grep -qE '^[[:space:]]*(export[[:space:]]+)?SUDO_PASSWORD[[:space:]]*=' .env 2>/dev/null; then
  echo "FAIL: SUDO_USERNAME exists but SUDO_PASSWORD is missing" >&2
  exit 2
fi

mapfile -t ADMIN_CREDS < <(python3 - <<'PY'
from pathlib import Path
import re
text = Path("/opt/marzban/.env").read_text()
def value(name):
    match = re.search(
        rf"^\s*(?:export\s+)?{name}\s*=\s*(.*)\s*$",
        text,
        flags=re.MULTILINE,
    )
    if not match:
        raise SystemExit(2)
    raw = match.group(1).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1]
    return raw
print(value("SUDO_USERNAME"))
print(value("SUDO_PASSWORD"))
PY
)
[[ "${#ADMIN_CREDS[@]}" -eq 2 && -n "${ADMIN_CREDS[0]}" && -n "${ADMIN_CREDS[1]}" ]] || {
  echo "FAIL: cannot safely read existing Marzban admin credentials" >&2
  exit 2
}
MARZBAN_ADMIN_USER="${ADMIN_CREDS[0]}"
MARZBAN_ADMIN_PASS="${ADMIN_CREDS[1]}"
unset ADMIN_CREDS

echo "[1/4] Importing current env sudo admin into Marzban database..."
docker compose exec -T marzban marzban-cli admin import-from-env -y </dev/null

echo "[2/4] Removing hard-coded sudo credentials from Marzban env..."
python3 - <<'PY'
from pathlib import Path
import os, re
path = Path("/opt/marzban/.env")
lines = path.read_text().splitlines()
filtered = [
    line for line in lines
    if not re.match(r"^\s*(?:export\s+)?SUDO_(?:USERNAME|PASSWORD)\s*=", line)
]
tmp = path.with_name(".env.db-admin-migration")
tmp.write_text("\n".join(filtered) + "\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY

echo "[3/4] Restarting Marzban with DB-backed admin..."
docker compose up -d --no-deps --force-recreate marzban </dev/null

READY=false
for _ in $(seq 1 30); do
  if curl -q -fsS --max-time 3 http://127.0.0.1:8000/openapi.json >/dev/null 2>&1; then
    READY=true
    break
  fi
  sleep 2
done
[ "$READY" = true ] || {
  echo "FAIL: Marzban API did not return after migration" >&2
  exit 2
}

echo "[4/4] Verifying the same credentials authenticate as DB admin..."
AUTH_CODE="$(curl -q -sS -o /dev/null -w '%{http_code}' --max-time 5   -X POST http://127.0.0.1:8000/api/admin/token   --data-urlencode "username=$MARZBAN_ADMIN_USER"   --data-urlencode "password=$MARZBAN_ADMIN_PASS" || true)"
unset MARZBAN_ADMIN_PASS
[[ "$AUTH_CODE" == 200 ]] || {
  echo "FAIL: DB-backed admin authentication returned HTTP $AUTH_CODE" >&2
  exit 2
}

rm -f "$BACKUP"
trap - EXIT
echo "PASS: env sudo migrated to DB; users and VPN credentials preserved."
