#!/usr/bin/env bash
# Low-memory deploy for the 1GB Private Line control plane.
# Never restarts/recreates PostgreSQL or nginx. Recreates application containers
# one at a time so restored bind mounts point at the current /opt/private-line.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

fail(){ echo "FAIL: $*" >&2; exit 2; }

[[ -f docker-compose.yml ]] || fail "docker-compose.yml missing"
docker ps --format '{{.Names}}' | grep -qx 'private-line-db-1' || fail "database container is not running"
docker ps --format '{{.Names}}' | grep -qx 'private-line-nginx-1' || fail "nginx container is not running"

AVAILABLE_KB="$(awk '/MemAvailable/ {print $2}' /proc/meminfo)"
if (( AVAILABLE_KB < 180000 )); then
  fail "less than ~176 MiB available RAM; refusing application recreation"
fi

echo "[1/6] Static checks only..."
python3 - <<'PY'
import ast
from pathlib import Path
for root in ("app","admin","bot","scheduler","health_monitor"):
    for p in Path(root).rglob("*.py"):
        ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
print("PASS: Python syntax")
PY
docker compose config -q
bash -n scripts/production_vpn_gate.sh
bash -n scripts/smoke_vpn_node.sh

echo "[2/6] Optional monitoring stays stopped..."
docker stop private-line-grafana-1 private-line-prometheus-1   private-line-alertmanager-1 private-line-node-exporter-1 >/dev/null 2>&1 || true

echo "[3/6] Recreate ADMIN only; DB and nginx untouched..."
docker compose up -d --no-deps --force-recreate admin
for _ in $(seq 1 45); do
  if curl -q -fsS --max-time 2 http://127.0.0.1:8000/_health >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -q -fsS --max-time 5 http://127.0.0.1:8000/_health >/dev/null   || fail "new admin container is not healthy"

echo "[4/6] Database migration through healthy ADMIN..."
docker compose exec -T admin python -m alembic upgrade head
curl -q -fsS --max-time 5 http://127.0.0.1:8000/_health >/dev/null   || fail "admin unhealthy after migration"

AVAILABLE_KB="$(awk '/MemAvailable/ {print $2}' /proc/meminfo)"
if (( AVAILABLE_KB < 140000 )); then
  fail "RAM too low after admin switch; BOT left untouched"
fi

echo "[5/6] Recreate BOT only..."
docker compose up -d --no-deps --force-recreate bot
sleep 5
docker ps --format '{{.Names}}' | grep -qx 'private-line-bot-1'   || fail "bot container is not running"

echo "[6/6] Runtime status..."
docker ps --filter name='private-line-'   --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
curl -q -fsS --max-time 5 http://127.0.0.1:8000/_health >/dev/null
echo "DEPLOY PASS: db/nginx were not restarted; admin/bot switched sequentially."
