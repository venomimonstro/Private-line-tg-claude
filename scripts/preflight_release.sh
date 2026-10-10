#!/usr/bin/env bash
# Safe pre-deploy checks. No CI/Actions and no VPN key changes.
# Run from /opt/private-line before restarting services.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

echo "[1/5] Checking Python syntax..."
python3 - <<'PY'
import ast
from pathlib import Path
for root in ("app", "bot", "admin", "scheduler"):
    for path in Path(root).rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
print("PASS: Python syntax")
PY

echo "[2/5] Checking shell scripts..."
for name in scripts/enable_primary_vpn.sh scripts/smoke_primary_vpn.sh \
    scripts/probe_vless_e2e.sh scripts/inspect_node_vless.sh; do
    bash -n "$name"
done
echo "PASS: shell syntax"

echo "[3/5] Testing VLESS share link formatting..."
python3 -m unittest discover -s tests -p 'test_vpn_link.py' -q

echo "[4/5] Validating existing Docker compose without changing containers..."
docker compose config -q

echo "[5/5] Verifying new backend module imports and existing admin endpoint..."
docker compose exec -T admin python -c \
    'from app.services.vpn_link import canonical_client_link; from app.services.server_access import add_subscription_server_key; from app.bootstrap_primary_node import apply; from app.primary_node_smoke import main; print("PASS: backend imports")'
status="$(curl -q -sS --max-time 8 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/_health)"
[[ "$status" == 200 ]] || { echo "FAIL: admin health HTTP $status"; exit 2; }
echo "PASS: existing admin health HTTP 200"
echo "PREDEPLOY PASS: code validation completed; VPN client access is a separate test."
