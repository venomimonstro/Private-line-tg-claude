#!/usr/bin/env bash
# Stable one-command entrypoint. This small bootstrap pulls the branch first and
# only then executes the freshly updated production orchestrator.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

BRANCH="${PRIVATE_LINE_BRANCH:-claude/private-line-mvp-cmx4Z}"

[[ -d .git ]] || { echo "FAIL: $ROOT is not a git checkout" >&2; exit 2; }
[[ -f .env ]] || { echo "FAIL: $ROOT/.env is missing" >&2; exit 2; }

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    echo "FAIL: tracked local changes exist; refusing to overwrite production checkout" >&2
    git status --short --untracked-files=no >&2
    exit 2
fi

BEFORE="$(git rev-parse HEAD)"
echo "=== GIT UPDATE: $BRANCH ==="
git fetch origin "$BRANCH"
git pull --ff-only origin "$BRANCH"
AFTER="$(git rev-parse HEAD)"

if [[ "$BEFORE" == "$AFTER" ]]; then
    export PRIVATE_LINE_CODE_CHANGED=0
    echo "Code already current: ${AFTER:0:8}"
else
    export PRIVATE_LINE_CODE_CHANGED=1
    echo "Code updated: ${BEFORE:0:8} -> ${AFTER:0:8}"
fi

exec bash scripts/update_production.sh "$@"
