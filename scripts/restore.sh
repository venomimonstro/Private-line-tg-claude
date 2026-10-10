#!/usr/bin/env bash
# Production restore entry point.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

BACKUP="${1:-}"
[[ -n "$BACKUP" ]] || {
  echo "Usage: $0 <backup.dump>" >&2
  echo "Available PostgreSQL backups:" >&2
  ls -1t backups/*.dump backups/*.db 2>/dev/null | head -20 >&2 || true
  exit 2
}
exec bash scripts/restore_postgres_backup.sh "$BACKUP"
