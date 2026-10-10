#!/usr/bin/env bash
# Compatibility wrapper. The old one-node stability procedure is retired.
# It referenced obsolete modules and bypassed the production acceptance gate.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

SUB_ID="${1:-3}"
[[ "$SUB_ID" =~ ^[1-9][0-9]*$ ]] || {
    echo "Usage: bash scripts/update_vpn_stability.sh [SUBSCRIPTION_ID]" >&2
    exit 2
}

echo "Deprecated wrapper: running the single supported Private Line production gate."
exec bash scripts/production_vpn_gate.sh "$SUB_ID"
