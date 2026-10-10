#!/usr/bin/env bash
# Backward-compatible alias for the single production acceptance gate.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
exec bash scripts/production_vpn_gate.sh "${1:-3}"
