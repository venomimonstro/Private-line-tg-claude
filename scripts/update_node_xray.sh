#!/usr/bin/env bash
# Compatibility wrapper. Production VPN runtime is converged exclusively by
# converge_vpn_node.sh so there is only one implementation of node state.
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Deprecated: update_node_xray.sh delegates to converge_vpn_node.sh"
exec bash "$SCRIPT_DIR/converge_vpn_node.sh"
