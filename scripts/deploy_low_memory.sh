#!/usr/bin/env bash
# Low-memory recovery/deploy for the Private Line control plane.
# Safe order: DB -> ADMIN -> migrations -> BOT -> NGINX.
# Existing healthy DB is never recreated/restarted.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

# Never let Docker recreate containers from a damaged/partial checkout.
bash scripts/validate_install_layout.sh

fail() {
  echo "FAIL: $*" >&2
  echo "--- admin/nginx/db status ---" >&2
  docker compose ps -a db admin bot nginx >&2 || true
  echo "--- recent admin/nginx errors ---" >&2
  docker compose logs --tail=80 admin nginx 2>&1 |     sed -E       -e 's/[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/[UUID]/g'       -e 's/(password|token|secret|privateKey)[=:][^[:space:]]+/\1=[REDACTED]/Ig'       -e 's@vless://[^[:space:]<]+@[VLESS_REDACTED]@g' >&2 || true
  exit 2
}

wait_http() {
  local url="$1"
  local tries="${2:-45}"
  for _ in $(seq 1 "$tries"); do
    if curl -q -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

start_existing_or_create() {
  local svc="$1"
  local cid
  cid="$(docker compose ps -a -q "$svc" 2>/dev/null || true)"
  if [[ -n "$cid" ]]; then
    if [[ "$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null || true)" != "true" ]]; then
      docker start "$cid" >/dev/null
    fi
  else
    docker compose up -d --no-deps "$svc"
  fi
}

[[ -f docker-compose.yml ]] || fail "docker-compose.yml missing"
[[ -f .env ]] || fail ".env missing"

for key in BOT_TOKEN ADMIN_CHAT_ID ENCRYPTION_KEY SECRET_KEY; do
  grep -qE "^${key}=.+" .env || fail "${key} is missing in .env"
done

echo "[1/8] Static checks..."
python3 - <<'PY'
import ast
from pathlib import Path
for root in ("app","admin","bot","scheduler","health_monitor"):
    base = Path(root)
    if not base.exists():
        continue
    for p in base.rglob("*.py"):
        ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
for p in Path("chrome_client/native").rglob("*.py"):
    ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
import json
json.loads(Path("chrome_client/extension/manifest.json").read_text(encoding="utf-8"))
print("PASS: Python syntax and Chrome extension manifest")
PY
docker compose config -q

echo "[2/8] Free RAM and optional monitoring..."
docker stop private-line-grafana-1 private-line-prometheus-1   private-line-alertmanager-1 private-line-node-exporter-1 >/dev/null 2>&1 || true
AVAILABLE_KB="$(awk '/MemAvailable/ {print $2}' /proc/meminfo)"
(( AVAILABLE_KB >= 140000 )) || fail "less than ~137 MiB available RAM"

echo "[3/8] Database..."
start_existing_or_create db
for _ in $(seq 1 45); do
  if docker compose exec -T db pg_isready -U privateline -d privateline >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
docker compose exec -T db pg_isready -U privateline -d privateline >/dev/null 2>&1   || fail "PostgreSQL is not ready"

echo "[4/8] Build immutable application runtime..."
# Application source is baked into private-line-app:runtime; it is no longer
# bind-mounted. A force-recreate without a build would silently keep old code.
docker compose build admin || fail "application runtime image build failed"

echo "[5/8] Admin..."
# Recreate only the stateless application container. DB remains untouched.
docker compose up -d --no-deps --force-recreate admin
wait_http http://127.0.0.1:8000/_health 45 || fail "FastAPI admin did not become healthy"

echo "[6/8] Database migrations..."
docker compose exec -T admin python -m alembic upgrade head
wait_http http://127.0.0.1:8000/_health 10 || fail "admin unhealthy after migration"
docker compose exec -T admin python - <<'PY'
import io
import json
import zipfile
from app.chrome_bundle import build_chrome_bundle, build_chrome_extension_zip
extension = build_chrome_extension_zip()
with zipfile.ZipFile(io.BytesIO(extension)) as only_extension:
    assert only_extension.testzip() is None
    assert "manifest.json" in only_extension.namelist()
    assert "extension/manifest.json" not in only_extension.namelist()
bundle = build_chrome_bundle()
with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
    assert archive.testzip() is None
    assert "native/bridge.py" in archive.namelist()
    assert "SETUP-WINDOWS.cmd" in archive.namelist()
    assert json.loads(archive.read("extension/manifest.json"))["manifest_version"] == 3
print("PASS: Chrome client ZIP is packaged and available inside admin/bot image")
from app.windows_bundle import build_windows_client
with zipfile.ZipFile(io.BytesIO(build_windows_client())) as win_archive:
    assert win_archive.testzip() is None
    assert "INSTALL-WINDOWS.cmd" in win_archive.namelist()
    assert "CHECK-INSTALL.cmd" in win_archive.namelist()
    assert "Check-Install.ps1" in win_archive.namelist()
    assert "Pair-PrivateLine.ps1" in win_archive.namelist()
    assert "PrivateLine.ps1" in win_archive.namelist()
    for powershell_file in ("Install-PrivateLine.ps1", "PrivateLine.ps1", "Check-Install.ps1"):
        assert win_archive.read(powershell_file).startswith(bytes((0xEF,0xBB,0xBF)))
print("PASS: Windows installer ZIP packaged with UTF-8 PowerShell scripts and verification launcher")
PY

echo "[7/8] Bot..."
AVAILABLE_KB="$(awk '/MemAvailable/ {print $2}' /proc/meminfo)"
(( AVAILABLE_KB >= 100000 )) || fail "RAM too low before bot recreation"

docker compose run --rm --no-deps bot python - <<'PY' || fail "bot import preflight failed; existing bot left untouched"
import bot.main
from bot.handlers import access, geo_switch, help, mykey, my_payments, payment, plans, referral, refund, rules, start, support, tickets
from bot.handlers import onboarding, payment_status, portal_link, qr_key, referral_withdraw, speed_test, stars_payment
print("PASS: bot imports")
PY

docker compose up -d --no-deps --force-recreate bot
sleep 10
BOT_CID="$(docker compose ps -q bot)"
[[ -n "$BOT_CID" ]] || fail "bot container missing"
BOT_RUNNING="$(docker inspect -f '{{.State.Running}}' "$BOT_CID" 2>/dev/null || true)"
BOT_RESTARTS="$(docker inspect -f '{{.RestartCount}}' "$BOT_CID" 2>/dev/null || echo 999)"
[[ "$BOT_RUNNING" == "true" ]] || fail "bot is not running"
[[ "$BOT_RESTARTS" == "0" ]] || fail "bot entered restart loop ($BOT_RESTARTS restarts)"
docker compose logs --since=15s bot 2>&1 | grep -q "Starting Private Line bot" \
  || fail "bot process started but polling startup marker was not observed"
echo "PASS: Telegram bot process stable after 10s, restart_count=$BOT_RESTARTS"

echo "[8/8] Nginx and public admin..."
start_existing_or_create nginx
docker compose exec -T nginx nginx -t >/dev/null 2>&1 || fail "nginx config is invalid"
docker compose exec -T nginx nginx -s reload >/dev/null 2>&1 || {
  docker compose restart nginx >/dev/null
}
wait_http http://127.0.0.1:29148/admin/login 20 || fail "admin proxy on :29148 is not healthy"

docker compose ps db admin bot nginx
echo "RECOVERY PASS: DB + admin + bot + nginx are healthy."
