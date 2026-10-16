#!/usr/bin/env bash
set -Eeuo pipefail
cd /opt/private-line
[[ -f .env && -d .git ]] || exit 0
exec 8>/var/lock/private-line-production-update.lock
flock -n 8 || exit 0
bash scripts/converge_https_fallback.sh --changed-only
