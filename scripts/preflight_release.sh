#!/usr/bin/env bash
# Safe production pre-deploy checks. No CI/Actions and no credential rotation.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

echo "[0/6] Checking installation layout before Docker..."
bash scripts/validate_install_layout.sh

echo "[1/6] Checking host-side Python syntax..."
python3 - <<'PY'
import ast
from pathlib import Path
for root in ("app", "bot", "admin", "scheduler", "health_monitor"):
    base=Path(root)
    if not base.exists():
        continue
    for path in base.rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
print("PASS: host Python syntax")
PY

echo "[2/6] Checking production shell scripts..."
for name in     install_node.sh     scripts/install_direct_xray_node.sh     scripts/converge_vpn_node.sh     scripts/prepare_marzban_runtime.sh     scripts/vpn_health_watch.sh     scripts/install_vpn_health_timer.sh     scripts/update_node_xray.sh     scripts/probe_vless_e2e.sh     scripts/inspect_node_vless.sh     scripts/smoke_vpn_node.sh     scripts/production_vpn_gate.sh     scripts/preflight_release.sh     scripts/update_vpn_stability.sh     scripts/repair_registered_node_ipv4.sh \
    scripts/validate_install_layout.sh \
    scripts/migrate_marzban_env_admin.sh \
    scripts/migrate_reality_target.sh \
    scripts/update_production.sh \
    update.sh \
    scripts/restore_postgres_backup.sh \
    scripts/restore.sh \
    scripts/verify_backup.sh; do
    bash -n "$name"
done
echo "PASS: shell syntax"

echo "[3/6] Validating Docker compose without changing containers..."
docker compose config -q
echo "PASS: Docker compose"

echo "[4/6] Checking production modules in the real admin container..."
docker compose exec -T admin python -m compileall -q     /app/app /app/bot /app/admin /app/scheduler /app/health_monitor
docker compose exec -T admin python -c '
from app.services.vpn_link import canonical_client_link
from app.services.vpn_reconcile import reconcile_marzban_server
from app.services.server_access import add_subscription_server_key
from app.stabilize_vpn_fleet import run
from app.audit_vpn_nodes import audit
from app.audit_data_integrity import audit as audit_data
from app.verify_control_plane import main as verify_control
from app.node_smoke_user import run as smoke_user
from app.promote_vpn_node import run as promote_node
from app.resync_vpn_node import run as resync_vpn_node
from app.payments.services import recover_paid_unfulfilled_payments
from app.services.promo import activate_tariff_trial
print("PASS: production backend imports")
'

echo "[5/6] Running unit tests inside production Python image..."
docker compose exec -T admin python -m unittest discover     -s /app/tests -p 'test_*.py' -q
echo "PASS: unit tests"

echo "[6/6] Checking current backend/admin without mutating VPN state..."
status="$(curl -q -sS --max-time 8 -o /dev/null -w '%{http_code}'     http://127.0.0.1:8000/_health)"
[[ "$status" == 200 ]] || { echo "FAIL: backend health HTTP $status"; exit 2; }
admin_status="$(curl -q -sS --max-time 8 -o /dev/null -w '%{http_code}'     http://127.0.0.1:29148/admin/login)"
[[ "$admin_status" == 200 ]] || { echo "FAIL: admin proxy HTTP $admin_status"; exit 2; }
echo "PASS: backend/admin HTTP 200"
echo "PREDEPLOY PASS"
