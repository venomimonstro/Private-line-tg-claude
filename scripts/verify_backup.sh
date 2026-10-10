#!/usr/bin/env bash
# Verify the latest real backup produced by backup_manager.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-./backups}"
LATEST="$(find "$BACKUP_DIR" -maxdepth 1 -type f \( -name 'backup_*.dump' -o -name 'backup_*.db' \) -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-)"
[[ -n "$LATEST" && -s "$LATEST" ]] || {
  echo "FAIL: no non-empty backup_manager backup found in $BACKUP_DIR" >&2
  exit 2
}

echo "Verifying: $LATEST"
echo "Size: $(du -h "$LATEST" | awk '{print $1}')"

DB_CID="$(docker compose ps -q db 2>/dev/null || true)"
if [[ -n "$DB_CID" ]] && docker compose exec -T db pg_restore --version >/dev/null 2>&1; then
  REMOTE="/tmp/private-line-verify-$$.dump"
  trap 'docker compose exec -T db rm -f "$REMOTE" >/dev/null 2>&1 || true' EXIT
  docker cp "$LATEST" "$DB_CID:$REMOTE" >/dev/null
  docker compose exec -T db pg_restore --list "$REMOTE" >/dev/null
  echo "PASS: PostgreSQL archive catalog is readable"
else
  python3 - "$LATEST" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
try:
    result = con.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise SystemExit("FAIL: SQLite integrity_check: " + str(result))
finally:
    con.close()
print("PASS: SQLite integrity_check")
PY
fi

AGE=$(( $(date +%s) - $(stat -c %Y "$LATEST") ))
if (( AGE > 90000 )); then
  echo "WARN: latest backup is older than 25 hours ($((AGE/3600))h)"
else
  echo "PASS: backup age $((AGE/3600))h"
fi

echo "BACKUP VERIFY PASS"
