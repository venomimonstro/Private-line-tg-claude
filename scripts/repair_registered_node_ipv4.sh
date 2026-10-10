#!/usr/bin/env bash
# Retired unsafe helper.
#
# Historical versions changed Marzban /api/hosts and the control-plane api_url
# independently, without disposable-user VLESS/HTTPS verification. That can
# publish a syntactically valid but unusable customer endpoint.
set -Eeuo pipefail

cat >&2 <<'EOF'
STOP: repair_registered_node_ipv4.sh is retired for production safety.

Do NOT patch a live VPN node IP with direct SQL or a standalone /api/hosts PUT.

Supported recovery:
  1. Correct/re-register the dedicated node through the Private Line node
     registration flow so it is staged (not selectable).
  2. On the control plane run:
       docker compose exec -T admin python -m app.stabilize_vpn_fleet --apply
       bash scripts/production_vpn_gate.sh <SUBSCRIPTION_ID>

The production gate performs disposable-user VLESS Reality + real HTTPS tests
before the node is published again. Existing UUIDs and Reality private keys are
preserved.
EOF
exit 2
