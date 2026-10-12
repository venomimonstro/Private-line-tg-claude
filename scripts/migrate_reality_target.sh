#!/usr/bin/env bash
# Compatibility wrapper for operators who previously used this command.
# The target override is fed into the single canonical golden-node converger.
set -Eeuo pipefail
TARGET_HOST="${1:-dl.google.com}"
case "$TARGET_HOST" in
  ""|*[!A-Za-z0-9.-]*) echo "FAIL: invalid REALITY target hostname" >&2; exit 2 ;;
esac
export REALITY_TARGET_HOST="$TARGET_HOST"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Deprecated: migrate_reality_target.sh delegates to converge_vpn_node.sh"
exec bash "$SCRIPT_DIR/converge_vpn_node.sh"
