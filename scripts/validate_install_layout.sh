#!/usr/bin/env bash
# Fail-closed validation of the control-plane checkout before Docker touches
# bind mounts. Docker Compose short bind syntax can otherwise create directories
# for missing sources, turning e.g. install_node.sh into a directory.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

fail() {
    echo "LAYOUT FAIL: $*" >&2
    exit 2
}

required_files=(
    .env
    docker-compose.yml
    Dockerfile
    requirements.txt
    alembic.ini
    install_node.sh
    fix_reality.sh
    nginx/nginx.conf
    app/config.py
    admin/main.py
    bot/main.py
    scripts/preflight_release.sh
)

required_dirs=(
    app
    admin
    bot
    backup_manager
    health_monitor
    update_manager
    scheduler
    alembic
    migrations
    scripts
    tests
    nginx
    nginx/ssl
    data
    backups
    logs
)

for path in "${required_files[@]}"; do
    [[ -f "$path" ]] || fail "required file is missing or not a regular file: $ROOT/$path"
done

for path in "${required_dirs[@]}"; do
    [[ -d "$path" ]] || fail "required directory is missing or not a directory: $ROOT/$path"
done

for key in BOT_TOKEN ADMIN_CHAT_ID ENCRYPTION_KEY SECRET_KEY POSTGRES_PASSWORD DATABASE_URL; do
    grep -qE "^${key}=.+" .env || fail "${key} is missing in .env"
done

# Refuse known placeholder secrets in production runtime.
if grep -qE '^POSTGRES_PASSWORD=(changeme|password|your_password_here)$' .env; then
    fail "POSTGRES_PASSWORD still uses a placeholder"
fi
if grep -qE '^SECRET_KEY=(test|changeme|your_secret_key_here)$' .env; then
    fail "SECRET_KEY still uses a placeholder"
fi

# A valid checkout must never contain the historical file-as-directory damage.
for path in install_node.sh fix_reality.sh nginx/nginx.conf docker-compose.yml; do
    [[ ! -d "$path" ]] || fail "file path was replaced by a directory: $ROOT/$path"
done

docker compose config -q

echo "LAYOUT PASS: control-plane checkout and bind sources are intact."
