#!/usr/bin/env bash
# Compatibility entrypoint. Production deployment has one source of truth:
# release.sh owns verified DB backup, migrations, health checks and rollback.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
BRANCH="${1:-$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)}"
exec bash "$ROOT/release.sh" --branch "$BRANCH"
