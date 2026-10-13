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

for script in \
    scripts/update_production.sh \
    scripts/install_direct_xray.sh \
    windows_native/build.sh \
    scripts/converge_vpn_node.sh \
    scripts/prepare_marzban_runtime.sh \
    scripts/vpn_health_watch.sh \
    scripts/install_vpn_health_timer.sh \
    scripts/update_node_xray.sh \
    scripts/migrate_reality_target.sh \
    scripts/inspect_node_vless.sh \
    scripts/smoke_vpn_node.sh \
    scripts/probe_vless_e2e.sh \
    scripts/production_vpn_gate.sh; do
    bash -n "$script" || {
        echo "FAIL: shell syntax check failed: $script" >&2
        exit 2
    }
done
echo "PASS: VPN rollout shell syntax"

exec bash scripts/update_production.sh "$@"
