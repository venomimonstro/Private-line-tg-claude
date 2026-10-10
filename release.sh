#!/usr/bin/env bash
# release.sh — Production release manager with validation, migration, health check, and rollback
#
# Usage:
#   ./release.sh                          # Deploy latest from current branch
#   ./release.sh --branch feature/x       # Deploy specific branch
#   ./release.sh --rollback <release_id>  # Roll back to a previous release
#   ./release.sh --list                   # List recent releases
#   ./release.sh --status                 # Show current release
#
# What this script does:
#   1. Saves current git state for rollback
#   2. Validates ALL Python files for syntax errors (BEFORE any changes)
#   3. Creates a pre-release database backup
#   4. Pulls new code
#   5. Validates NEW Python files for syntax errors
#   6. Runs database migrations (alembic upgrade head)
#   7. Restarts bot and admin containers
#   8. Health-checks admin for up to 40 seconds
#   9. On any failure → automatic rollback to previous commit + DB restore
#  10. Records the release outcome in the DB

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
HEALTH_URL="http://localhost/_health"
HEALTH_RETRIES=10
HEALTH_WAIT=4
RELEASE_ID=""
ROLLBACK_MODE=0
LIST_MODE=0
STATUS_MODE=0
BRANCH=""

cd "$DEPLOY_DIR"

# Abort before backup/deploy if the checkout cannot safely back Docker bind mounts.
bash scripts/validate_install_layout.sh

# ── argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --rollback) ROLLBACK_MODE=1; RELEASE_ID="${2:-}"; shift 2 ;;
        --branch)   BRANCH="$2"; shift 2 ;;
        --list)     LIST_MODE=1; shift ;;
        --status)   STATUS_MODE=1; shift ;;
        -h|--help)
            sed -n '/^# Usage:/,/^$/p' "$0" | head -20
            exit 0
            ;;
        *) BRANCH="$1"; shift ;;
    esac
done

BRANCH="${BRANCH:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")}"

# ── helpers ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "[$(date '+%H:%M:%S')] $*"; }
ok()   { echo -e "[$(date '+%H:%M:%S')] ${GREEN}✅ $*${NC}"; }
err()  { echo -e "[$(date '+%H:%M:%S')] ${RED}❌ $*${NC}" >&2; }
warn() { echo -e "[$(date '+%H:%M:%S')] ${YELLOW}⚠️  $*${NC}"; }
bold() { echo -e "${BOLD}$*${NC}"; }

py_cli() { python3 -m releases.cli "$@"; }

# ── list mode ─────────────────────────────────────────────────────────────────
if [[ "$LIST_MODE" -eq 1 ]]; then
    py_cli list
    exit 0
fi

# ── status mode ───────────────────────────────────────────────────────────────
if [[ "$STATUS_MODE" -eq 1 ]]; then
    bold "Current git commit: $(git rev-parse --short HEAD)"
    bold "Current branch:     $(git rev-parse --abbrev-ref HEAD)"
    py_cli list | head -5
    exit 0
fi

# ── rollback mode ─────────────────────────────────────────────────────────────
if [[ "$ROLLBACK_MODE" -eq 1 ]]; then
    if [[ -z "$RELEASE_ID" ]]; then
        err "Usage: ./release.sh --rollback <release_id>"
        exit 1
    fi

    bold "════════════════════════════════════════════"
    bold "  SAFE ROLLBACK release #$RELEASE_ID"
    bold "════════════════════════════════════════════"

    PLAN="$(py_cli rollback-plan "$RELEASE_ID" 2>/tmp/rollback_plan_err)" || {
        err "$(cat /tmp/rollback_plan_err 2>/dev/null || echo 'Rollback plan unavailable')"
        err "Historical rollback is refused without an associated verified DB backup."
        exit 2
    }
    IFS='|' read -r PREV_COMMIT BACKUP_ID BACKUP_NAME BACKUP_SHA <<<"$PLAN"
    [[ "$PREV_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]] || { err "Invalid rollback commit"; exit 2; }
    [[ "$BACKUP_ID" =~ ^[1-9][0-9]*$ ]] || { err "Invalid rollback backup id"; exit 2; }
    ROLLBACK_BACKUP="$DEPLOY_DIR/backups/$BACKUP_NAME"
    [[ -s "$ROLLBACK_BACKUP" ]] || { err "Rollback backup missing/empty: $ROLLBACK_BACKUP"; exit 2; }
    ACTUAL_SHA="$(sha256sum "$ROLLBACK_BACKUP" | awk '{print $1}')"
    [[ -n "$BACKUP_SHA" && "$ACTUAL_SHA" == "$BACKUP_SHA" ]] || { err "Rollback backup checksum mismatch"; exit 2; }

    HELPER="/tmp/private-line-manual-rollback-$$.sh"
    cp -p scripts/restore_postgres_backup.sh "$HELPER"
    chmod 700 "$HELPER"

    log "Rolling code back to ${PREV_COMMIT:0:7} and restoring its matching DB snapshot..."
    git reset --hard "$PREV_COMMIT" >/dev/null || { rm -f "$HELPER"; err "Git rollback failed"; exit 2; }

    if ! PRIVATE_LINE_ROOT="$DEPLOY_DIR" PRIVATE_LINE_SKIP_LAYOUT_VALIDATION=1 \
        bash "$HELPER" "$ROLLBACK_BACKUP"; then
        rm -f "$HELPER"
        err "Database rollback failed; current emergency dump path is printed above."
        exit 2
    fi
    rm -f "$HELPER"

    if curl -sf --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
        ok "Rollback complete — code and PostgreSQL restored together"
    else
        err "Rollback completed but admin is unhealthy"
        exit 2
    fi
    exit 0
fi
# ═════════════════════════════════════════════════════════════════════════════
# NORMAL DEPLOY
# ═════════════════════════════════════════════════════════════════════════════

bold "════════════════════════════════════════════"
bold "  Private Line — Release Deploy"
bold "  Branch: $BRANCH"
bold "════════════════════════════════════════════"

PREV_COMMIT=$(git rev-parse HEAD)
log "Current commit: ${PREV_COMMIT:0:7} ($(git rev-parse --abbrev-ref HEAD))"

# ── Step 1: validate CURRENT code first (sanity check) ───────────────────────
log "🔍 [1/8] Validating current Python files..."
SYNTAX_OK=1
CHECKED=0
while IFS= read -r -d '' pyfile; do
    CHECKED=$(( CHECKED + 1 ))
    if ! python3 -m py_compile "$pyfile" 2>/tmp/syntax_err; then
        err "Syntax error in $pyfile: $(cat /tmp/syntax_err)"
        SYNTAX_OK=0
    fi
done < <(find app bot admin backup_manager health_monitor update_manager releases \
         -name "*.py" -not -path "*/\.*" -print0 2>/dev/null)
[[ "$SYNTAX_OK" -eq 1 ]] && ok "Current code valid: $CHECKED files" || fail "Current checkout has Python syntax errors; refusing deployment"

# ── Step 2: Create mandatory pre-release DB backup ────────────────────────────
log "💾 [2/8] Creating mandatory pre-release database backup..."
BACKUP_META="$(docker compose exec -T admin python3 -c "
import asyncio
from pathlib import Path
from app.database import async_session_maker
from app.enums import BackupType
from backup_manager.backup import create_backup
async def _():
    async with async_session_maker() as s:
        b = await create_backup(BackupType.pre_update, s, created_by='release.sh')
        print(f'{b.id}|{Path(b.file_path).name}')
asyncio.run(_())
" 2>/tmp/backup_err)" || {
    err "Pre-release backup failed: $(tail -3 /tmp/backup_err 2>/dev/null || true)"
    fail "Deployment aborted before code changes because rollback would be unsafe"
}
BACKUP_META="$(printf '%s\n' "$BACKUP_META" | tail -1 | tr -d '\r')"
BACKUP_ID="${BACKUP_META%%|*}"
BACKUP_NAME="${BACKUP_META#*|}"
[[ "$BACKUP_ID" =~ ^[1-9][0-9]*$ ]] || fail "Invalid backup ID returned by backup manager"
[[ -n "$BACKUP_NAME" && "$BACKUP_NAME" != "$BACKUP_META" ]] || fail "Invalid backup filename returned by backup manager"
PRE_RELEASE_BACKUP="$DEPLOY_DIR/backups/$BACKUP_NAME"
[[ -s "$PRE_RELEASE_BACKUP" ]] || fail "Pre-release backup file is missing or empty: $PRE_RELEASE_BACKUP"
ok "Backup created and verified: $BACKUP_NAME"
# ── Step 3: Fetch new code ────────────────────────────────────────────────────
log "⬇  [3/8] Pulling code from '$BRANCH'..."
git fetch origin "$BRANCH" 2>&1 | sed 's/^/    /'
git checkout "$BRANCH" 2>/dev/null || true
git pull origin "$BRANCH" 2>&1 | sed 's/^/    /'

NEW_COMMIT=$(git rev-parse HEAD)

if [[ "$PREV_COMMIT" == "$NEW_COMMIT" ]]; then
    ok "No changes — already up to date"
    docker compose exec -T nginx nginx -s reload 2>/dev/null || true
    exit 0
fi

log "New commit: ${NEW_COMMIT:0:7}"
CHANGELOG=$(git log --oneline "${PREV_COMMIT}..${NEW_COMMIT}" 2>/dev/null | head -30 || true)
echo "$CHANGELOG" | sed 's/^/    ► /'

# ── Step 4: Register release in DB ───────────────────────────────────────────
log "📋 [4/8] Registering release in database..."
RELEASE_ID=$(py_cli create \
    --prev "$PREV_COMMIT" \
    --commit "$NEW_COMMIT" \
    --version "$(date '+%Y.%m.%d')-${NEW_COMMIT:0:7}" \
    --changelog "$CHANGELOG" \
    --backup-id "$BACKUP_ID" \
    2>/tmp/cli_err) || { warn "DB registration failed: $(cat /tmp/cli_err)"; RELEASE_ID=""; }
[[ -n "$RELEASE_ID" ]] && ok "Release #$RELEASE_ID registered" || warn "Continuing without DB tracking"

# ── Auto-rollback function ────────────────────────────────────────────────────
# Keep an independent restore helper because git rollback may change/remove it.
ROLLBACK_RESTORE_HELPER="/tmp/private-line-db-rollback-$$.sh"
cp -p scripts/restore_postgres_backup.sh "$ROLLBACK_RESTORE_HELPER"
chmod 700 "$ROLLBACK_RESTORE_HELPER"
MIGRATION_TOUCHED=0

auto_rollback() {
    local reason="$1"
    err "DEPLOY FAILED: $reason"
    warn "═══ STARTING AUTOMATIC ROLLBACK ═══"

    if [[ -n "$RELEASE_ID" ]]; then
        py_cli fail "$RELEASE_ID" --error "$reason" 2>/dev/null || true
    fi

    warn "Reverting code to ${PREV_COMMIT:0:7}..."
    git reset --hard "$PREV_COMMIT" >/dev/null 2>&1 || {
        err "FATAL: Git rollback failed"
        exit 2
    }

    if [[ "$MIGRATION_TOUCHED" -eq 1 ]]; then
        warn "Database migration was attempted; restoring pre-release dump..."
        if ! PRIVATE_LINE_ROOT="$DEPLOY_DIR" PRIVATE_LINE_SKIP_LAYOUT_VALIDATION=1 \
            bash "$ROLLBACK_RESTORE_HELPER" "$PRE_RELEASE_BACKUP"; then
            err "FATAL: database rollback failed. Backup: $PRE_RELEASE_BACKUP"
            exit 2
        fi
    else
        docker compose restart admin bot >/dev/null 2>&1 || true
    fi

    rm -f "$ROLLBACK_RESTORE_HELPER"
    sleep 3
    if curl -sf --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
        ok "Rollback successful — code and required database state restored"
    else
        err "Rollback completed but admin is unhealthy — manual intervention required"
        exit 2
    fi
    exit 1
}
# ── Step 5: Validate NEW Python files ────────────────────────────────────────
log "🔍 [5/8] Validating new Python files..."
SYNTAX_ERRORS=0
CHECKED=0
SYNTAX_LOG=""
while IFS= read -r -d '' pyfile; do
    CHECKED=$(( CHECKED + 1 ))
    if ! ERROR=$(python3 -m py_compile "$pyfile" 2>&1); then
        SYNTAX_ERRORS=$(( SYNTAX_ERRORS + 1 ))
        SYNTAX_LOG+="ERROR in $pyfile: $ERROR\n"
        err "  Syntax error: $pyfile"
        echo "  $ERROR"
    fi
done < <(find app bot admin backup_manager health_monitor update_manager releases \
         -name "*.py" -not -path "*/\.*" -print0 2>/dev/null)

if [[ "$SYNTAX_ERRORS" -gt 0 ]]; then
    auto_rollback "Found $SYNTAX_ERRORS Python syntax errors — deploy aborted before any changes"
fi
ok "New code valid: $CHECKED files, 0 errors"

# ── Step 6: Run DB migrations ─────────────────────────────────────────────────
log "🗄  [6/8] Running database migrations..."
# A failing Alembic invocation may already have committed an earlier revision.
MIGRATION_TOUCHED=1
if ! docker compose exec -T admin alembic upgrade head 2>&1 | tee /tmp/alembic_log | sed 's/^/    /'; then
    ALEMBIC_ERR=$(cat /tmp/alembic_log | tail -10)
    auto_rollback "Database migration failed: $ALEMBIC_ERR"
fi
ok "Migrations applied"

# ── Step 7: Restart services ──────────────────────────────────────────────────
log "🔄 [7/8] Restarting services..."
docker compose restart admin bot 2>&1 | sed 's/^/    /'

# reload nginx gracefully
docker compose exec -T nginx nginx -s reload 2>/dev/null || docker compose restart nginx 2>/dev/null || true

# ── Step 8: Health check ──────────────────────────────────────────────────────
log "🏥 [8/8] Health check (up to $((HEALTH_RETRIES * HEALTH_WAIT))s)..."
sleep 6  # give services time to boot

HEALTHY=0
for i in $(seq 1 "$HEALTH_RETRIES"); do
    if curl -sf --max-time 5 "$HEALTH_URL" > /dev/null 2>&1; then
        HEALTHY=1; break
    fi
    log "   attempt $i/$HEALTH_RETRIES..."
    sleep "$HEALTH_WAIT"
done

if [[ "$HEALTHY" -eq 0 ]]; then
    warn "Admin logs:"
    docker compose logs admin --tail=25 2>/dev/null | sed 's/^/    /' || true
    auto_rollback "Admin health check failed after $((HEALTH_RETRIES * HEALTH_WAIT))s"
fi

# ── Mark release as successful ────────────────────────────────────────────────
rm -f "$ROLLBACK_RESTORE_HELPER"
if [[ -n "$RELEASE_ID" ]]; then
    DEPLOY_LOG="Git pull OK. Migrations OK. Services restarted. Health check passed."
    py_cli complete "$RELEASE_ID" --log "$DEPLOY_LOG" 2>/dev/null || true
fi

bold ""
ok "════════════════════════════════════════════"
ok "  Release #${RELEASE_ID:-N/A} DEPLOYED SUCCESSFULLY!"
ok "  Commit: ${NEW_COMMIT:0:7} ← ${PREV_COMMIT:0:7}"
ok "  To rollback: ./release.sh --rollback ${RELEASE_ID:-<id>}"
ok "════════════════════════════════════════════"

log ""
log "Recent releases:"
py_cli list 2>/dev/null | head -10 || true
