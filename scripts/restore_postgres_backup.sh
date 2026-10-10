#!/usr/bin/env bash
# Transaction-like PostgreSQL restore orchestrated from the Docker host.
# Usage: bash scripts/restore_postgres_backup.sh backups/backup_manual_....dump
set -Eeuo pipefail
umask 077
ROOT="${PRIVATE_LINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT"

BACKUP="${1:-}"
[[ -n "$BACKUP" ]] || { echo "Usage: $0 <postgres-custom-dump>" >&2; exit 2; }
[[ -f "$BACKUP" && -s "$BACKUP" ]] || { echo "FAIL: backup missing/empty: $BACKUP" >&2; exit 2; }

if [[ "${PRIVATE_LINE_SKIP_LAYOUT_VALIDATION:-0}" == 1 ]]; then
  [[ -f docker-compose.yml && -f .env ]] || {
    echo "FAIL: minimal restore layout missing under $ROOT" >&2
    exit 2
  }
else
  bash scripts/validate_install_layout.sh
fi

DB_CID="$(docker compose ps -q db)"
[[ -n "$DB_CID" ]] || { echo "FAIL: PostgreSQL container not found" >&2; exit 2; }
DB_USER="$(docker inspect "$DB_CID" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^POSTGRES_USER=//p' | head -1)"
DB_NAME="$(docker inspect "$DB_CID" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^POSTGRES_DB=//p' | head -1)"
[[ -n "$DB_USER" && -n "$DB_NAME" ]] || { echo "FAIL: cannot determine PostgreSQL database/user" >&2; exit 2; }
[[ "$DB_NAME" =~ ^[A-Za-z0-9_]+$ ]] || { echo "FAIL: unsafe PostgreSQL database name" >&2; exit 2; }

docker compose exec -T db pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null

REMOTE="/tmp/private-line-restore-$$.dump"
EMERGENCY="/root/private-line-pre-restore-$(date -u +%Y%m%dT%H%M%SZ)-$$.dump"
APP_WAS_ADMIN="$(docker inspect -f '{{.State.Running}}' private-line-admin-1 2>/dev/null || echo false)"
APP_WAS_BOT="$(docker inspect -f '{{.State.Running}}' private-line-bot-1 2>/dev/null || echo false)"

cleanup() {
  docker compose exec -T db rm -f "$REMOTE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[1/6] Verifying PostgreSQL archive..."
docker cp "$BACKUP" "$DB_CID:$REMOTE"
docker compose exec -T db pg_restore --list "$REMOTE" >/dev/null

echo "[2/6] Creating emergency pre-restore dump..."
docker compose exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$EMERGENCY"
[[ -s "$EMERGENCY" ]] || { echo "FAIL: emergency backup is empty" >&2; exit 2; }

echo "[3/6] Stopping application writers..."
docker stop private-line-bot-1 private-line-admin-1 >/dev/null 2>&1 || true

terminate_connections() {
  docker compose exec -T db psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1     -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DB_NAME' AND pid <> pg_backend_pid();"     >/dev/null
}

restore_file() {
  local host_file="$1"
  docker cp "$host_file" "$DB_CID:$REMOTE"
  terminate_connections
  docker compose exec -T db pg_restore     -U "$DB_USER" -d "$DB_NAME"     --clean --if-exists --no-owner --no-privileges --exit-on-error     "$REMOTE"
}

echo "[4/6] Restoring requested backup..."
if ! restore_file "$BACKUP"; then
  echo "RESTORE FAILED: reverting database from emergency dump..." >&2
  if restore_file "$EMERGENCY"; then
    echo "Emergency database rollback succeeded." >&2
  else
    echo "FATAL: emergency database rollback also failed: $EMERGENCY" >&2
  fi
  [[ "$APP_WAS_ADMIN" == true ]] && docker start private-line-admin-1 >/dev/null 2>&1 || true
  [[ "$APP_WAS_BOT" == true ]] && docker start private-line-bot-1 >/dev/null 2>&1 || true
  exit 1
fi

echo "[5/6] Applying migrations for the currently checked-out code..."
docker compose run --rm --no-deps admin alembic upgrade head </dev/null

echo "[6/6] Starting application and validating health..."
[[ "$APP_WAS_ADMIN" == true ]] && docker start private-line-admin-1 >/dev/null
for _ in $(seq 1 30); do
  curl -q -fsS --max-time 2 http://127.0.0.1:8000/_health >/dev/null 2>&1 && break
  sleep 2
done
if [[ "$APP_WAS_ADMIN" == true ]] && ! curl -q -fsS --max-time 3 http://127.0.0.1:8000/_health >/dev/null; then
  echo "FAIL: database restored but admin did not become healthy; emergency dump: $EMERGENCY" >&2
  exit 2
fi
[[ "$APP_WAS_BOT" == true ]] && docker start private-line-bot-1 >/dev/null

echo "PASS: PostgreSQL restore completed and validated."
echo "Emergency pre-restore dump retained: $EMERGENCY"
