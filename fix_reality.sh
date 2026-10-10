#!/usr/bin/env bash
# Deprecated destructive helper.
#
# Historical versions regenerated Reality x25519 keys and mass-updated Marzban
# users. That invalidates previously issued client profiles and is not safe for
# a production VPN service.
set -euo pipefail

cat >&2 <<'EOF'
STOP: fix_reality.sh is intentionally disabled.

It is unsafe for a live Private Line node because it rotates Reality credentials.

Use the control-plane reconciliation instead:
  docker compose exec -T admin python -m app.stabilize_vpn_fleet --apply

Then verify:
  docker compose exec -T admin python -m app.audit_vpn_nodes
  bash scripts/probe_vless_e2e.sh <SUBSCRIPTION_ID>

No UUID or Reality private key will be rotated by the supported repair path.
EOF
exit 2
