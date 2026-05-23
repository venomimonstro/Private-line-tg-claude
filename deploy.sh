#!/usr/bin/env bash
# deploy.sh — sprint-based deployment with validation and auto-rollback
#
# Usage:
#   ./deploy.sh                        # deploy latest from current branch
#   ./deploy.sh claude/private-line-mvp-cmx4Z   # deploy specific branch

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
BRANCH="${1:-$(git -C "$DEPLOY_DIR" rev-parse --abbrev-ref HEAD)}"
HEALTH_URL="http://localhost/_health"
HEALTH_RETRIES=8
HEALTH_WAIT=4
SPRINT_DIR="$DEPLOY_DIR/sprints"

cd "$DEPLOY_DIR"

# ── helpers ──────────────────────────────────────────────────────────────────
log()  { echo "[$(date '+%H:%M:%S')] $*"; }
ok()   { echo "[$(date '+%H:%M:%S')] ✅ $*"; }
fail() { echo "[$(date '+%H:%M:%S')] ❌ $*" >&2; exit 1; }
warn() { echo "[$(date '+%H:%M:%S')] ⚠️  $*"; }

# ── sprint number ─────────────────────────────────────────────────────────────
mkdir -p "$SPRINT_DIR"
SPRINT_NUM=$(( $(ls "$SPRINT_DIR" 2>/dev/null | grep -c '^sprint_' || echo 0) + 1 ))
SPRINT_NAME="sprint_${SPRINT_NUM}"
SPRINT_PATH="$SPRINT_DIR/$SPRINT_NAME"

log "════════════════════════════════════════════"
log "  Деплой $SPRINT_NAME  |  ветка: $BRANCH"
log "════════════════════════════════════════════"

# ── save current state for rollback ──────────────────────────────────────────
PREV_COMMIT=$(git rev-parse HEAD)
PREV_BRANCH=$(git rev-parse --abbrev-ref HEAD)
log "Текущая версия: $PREV_COMMIT ($PREV_BRANCH)"

# Save rollback marker
mkdir -p "$SPRINT_PATH"
echo "$PREV_COMMIT" > "$SPRINT_PATH/.rollback_commit"
echo "$PREV_BRANCH" > "$SPRINT_PATH/.rollback_branch"

rollback() {
    warn "Откатываюсь на предыдущую версию ($PREV_COMMIT)..."
    git checkout "$PREV_COMMIT" -- . 2>/dev/null || git checkout "$PREV_BRANCH" 2>/dev/null || true
    docker compose restart admin bot 2>/dev/null || true
    rm -rf "$SPRINT_PATH"
    fail "$1"
}

# ── pull new code ─────────────────────────────────────────────────────────────
log "⬇  Получаю обновления ветки '$BRANCH'..."
git fetch origin "$BRANCH" 2>&1 | sed 's/^/    /'
git checkout "$BRANCH" 2>/dev/null || true
git pull origin "$BRANCH" 2>&1 | sed 's/^/    /'

NEW_COMMIT=$(git rev-parse HEAD)

if [ "$PREV_COMMIT" = "$NEW_COMMIT" ]; then
    ok "Изменений нет (уже актуальная версия)"
    rm -rf "$SPRINT_PATH"
    exit 0
fi

log "Новая версия: $NEW_COMMIT"
echo "$NEW_COMMIT" > "$SPRINT_PATH/.deploy_commit"
git log --oneline "$PREV_COMMIT..$NEW_COMMIT" 2>/dev/null | sed 's/^/    /' || true

# ── validate Python syntax ───────────────────────────────────────────────────
log "🔍 Проверяю синтаксис Python..."
SYNTAX_ERRORS=0
CHECKED=0

while IFS= read -r -d '' pyfile; do
    if ! python3 -c "
import ast, sys
try:
    ast.parse(open('$pyfile').read())
except SyntaxError as e:
    print(f'  СИНТАКСИЧЕСКАЯ ОШИБКА: $pyfile:{e.lineno}: {e.msg}')
    sys.exit(1)
" 2>&1; then
        SYNTAX_ERRORS=$(( SYNTAX_ERRORS + 1 ))
    fi
    CHECKED=$(( CHECKED + 1 ))
done < <(find app bot admin backup_manager health_monitor update_manager -name "*.py" -print0 2>/dev/null)

if [ "$SYNTAX_ERRORS" -gt 0 ]; then
    rollback "Найдено $SYNTAX_ERRORS синтаксических ошибок — деплой отменён"
fi
ok "Синтаксис Python: $CHECKED файлов проверено, ошибок нет"

# ── apply: restart services (code mounted via volumes — no rebuild) ───────────
log "🔄 Перезапускаю сервисы..."
docker compose restart admin bot 2>&1 | sed 's/^/    /'

# ── wait for admin to become healthy ─────────────────────────────────────────
log "⏳ Жду запуска admin (до $(( HEALTH_RETRIES * HEALTH_WAIT )) сек)..."
sleep 5

HEALTHY=0
for i in $(seq 1 $HEALTH_RETRIES); do
    if curl -sf --max-time 5 "$HEALTH_URL" > /dev/null 2>&1; then
        HEALTHY=1
        break
    fi
    log "   попытка $i/$HEALTH_RETRIES..."
    sleep "$HEALTH_WAIT"
done

if [ "$HEALTHY" -eq 0 ]; then
    warn "Admin не отвечает после обновления. Проверяю логи..."
    docker compose logs admin --tail=20 2>/dev/null | sed 's/^/    /' || true
    rollback "Admin не запустился — автооткат выполнен"
fi

# ── success ───────────────────────────────────────────────────────────────────
echo "$NEW_COMMIT" > "$SPRINT_PATH/.success"
echo "$SPRINT_NAME" > "$DEPLOY_DIR/.current_sprint"

ok "════════════════════════════════════════════"
ok "  $SPRINT_NAME успешно задеплоен!"
ok "  Коммит: $NEW_COMMIT"
ok "  Предыдущий: $PREV_COMMIT"
ok "════════════════════════════════════════════"

# Show sprint history
log ""
log "История спринтов:"
ls "$SPRINT_DIR" 2>/dev/null | while read -r s; do
    commit_file="$SPRINT_DIR/$s/.deploy_commit"
    success_file="$SPRINT_DIR/$s/.success"
    commit=""
    status="⚠️  (отменён)"
    [ -f "$commit_file" ] && commit="$(cat "$commit_file" | cut -c1-7)"
    [ -f "$success_file" ] && status="✅"
    current=""
    [ "$(cat "$DEPLOY_DIR/.current_sprint" 2>/dev/null)" = "$s" ] && current=" ← текущий"
    echo "  $status $s  $commit$current"
done
