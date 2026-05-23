#!/usr/bin/env bash
# deploy.sh — sprint-based deployment with validation and auto-rollback
#
# Usage:
#   ./deploy.sh                                    # latest from current branch
#   ./deploy.sh claude/private-line-mvp-cmx4Z     # specific branch

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
BRANCH="${1:-$(git -C "$DEPLOY_DIR" rev-parse --abbrev-ref HEAD)}"
HEALTH_URL="http://localhost/_health"
HEALTH_RETRIES=8
HEALTH_WAIT=4
SPRINT_DIR="$DEPLOY_DIR/sprints"

cd "$DEPLOY_DIR"

# ── helpers ───────────────────────────────────────────────────────────────────
log()  { echo "[$(date '+%H:%M:%S')] $*"; }
ok()   { echo "[$(date '+%H:%M:%S')] ✅ $*"; }
err()  { echo "[$(date '+%H:%M:%S')] ❌ $*" >&2; }
warn() { echo "[$(date '+%H:%M:%S')] ⚠️  $*"; }

fail() {
    err "$1"
    exit 1
}

# ── sprint number (safe count) ────────────────────────────────────────────────
mkdir -p "$SPRINT_DIR"
SPRINT_COUNT=0
for d in "$SPRINT_DIR"/sprint_*/; do
    [ -d "$d" ] && SPRINT_COUNT=$(( SPRINT_COUNT + 1 ))
done
SPRINT_NUM=$(( SPRINT_COUNT + 1 ))
SPRINT_NAME="sprint_${SPRINT_NUM}"
SPRINT_PATH="$SPRINT_DIR/$SPRINT_NAME"
mkdir -p "$SPRINT_PATH"

log "════════════════════════════════════════════"
log "  Деплой $SPRINT_NAME  |  ветка: $BRANCH"
log "════════════════════════════════════════════"

# ── save current commit for rollback ─────────────────────────────────────────
PREV_COMMIT=$(git rev-parse HEAD)
PREV_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "$PREV_COMMIT")
log "Текущая версия: ${PREV_COMMIT:0:7} ($PREV_BRANCH)"
echo "$PREV_COMMIT" > "$SPRINT_PATH/.rollback_commit"

rollback() {
    warn "Откат на $PREV_COMMIT..."
    git checkout "$PREV_COMMIT" -- . 2>/dev/null || true
    docker compose restart admin bot 2>/dev/null || true
    rm -rf "$SPRINT_PATH"
    fail "$1"
}

# ── pull new code ─────────────────────────────────────────────────────────────
log "⬇  Получаю обновления '$BRANCH'..."
git fetch origin "$BRANCH" 2>&1 | sed 's/^/    /'
git checkout "$BRANCH" 2>/dev/null || true
git pull origin "$BRANCH" 2>&1 | sed 's/^/    /'

NEW_COMMIT=$(git rev-parse HEAD)

if [ "$PREV_COMMIT" = "$NEW_COMMIT" ]; then
    ok "Изменений нет — уже актуальная версия"
    docker compose exec -T nginx nginx -s reload 2>/dev/null || true
    rm -rf "$SPRINT_PATH"
    exit 0
fi

log "Новая версия:  ${NEW_COMMIT:0:7}"
echo "$NEW_COMMIT" > "$SPRINT_PATH/.deploy_commit"
git log --oneline "${PREV_COMMIT}..${NEW_COMMIT}" 2>/dev/null | sed 's/^/    ► /' || true

# ── validate Python syntax ────────────────────────────────────────────────────
log "🔍 Проверяю синтаксис Python..."
SYNTAX_ERRORS=0
CHECKED=0

while IFS= read -r -d '' pyfile; do
    CHECKED=$(( CHECKED + 1 ))
    if ! python3 -c "
import ast, sys
try:
    ast.parse(open(sys.argv[1]).read())
except SyntaxError as e:
    print(f'  ОШИБКА {sys.argv[1]}:{e.lineno}: {e.msg}')
    sys.exit(1)
" "$pyfile" 2>&1; then
        SYNTAX_ERRORS=$(( SYNTAX_ERRORS + 1 ))
    fi
done < <(find app bot admin backup_manager health_monitor update_manager -name "*.py" -print0 2>/dev/null)

if [ "$SYNTAX_ERRORS" -gt 0 ]; then
    rollback "Найдено $SYNTAX_ERRORS синтаксических ошибок — деплой отменён"
fi
ok "Python OK: $CHECKED файлов проверено"

# ── restart services (code via volumes — no rebuild needed) ───────────────────
log "🔄 Перезапускаю сервисы..."
docker compose restart admin bot 2>&1 | sed 's/^/    /'

# ── reload nginx (graceful — applies new config without downtime) ─────────────
log "🔄 Перезагружаю nginx..."
if docker compose exec -T nginx nginx -s reload 2>&1 | sed 's/^/    /'; then
    ok "nginx перезагружен"
else
    warn "nginx reload не удался — пробую restart..."
    docker compose restart nginx 2>&1 | sed 's/^/    /' || true
fi

# ── health check ──────────────────────────────────────────────────────────────
log "⏳ Жду запуска admin..."
sleep 5

HEALTHY=0
for i in $(seq 1 "$HEALTH_RETRIES"); do
    if curl -sf --max-time 5 "$HEALTH_URL" > /dev/null 2>&1; then
        HEALTHY=1
        break
    fi
    log "   попытка $i/$HEALTH_RETRIES..."
    sleep "$HEALTH_WAIT"
done

if [ "$HEALTHY" -eq 0 ]; then
    warn "Admin не отвечает. Последние логи:"
    docker compose logs admin --tail=15 2>/dev/null | sed 's/^/    /' || true
    rollback "Admin не запустился — выполнен автооткат"
fi

# ── success ───────────────────────────────────────────────────────────────────
echo "$NEW_COMMIT" > "$SPRINT_PATH/.success"
echo "$SPRINT_NAME" > "$DEPLOY_DIR/.current_sprint"

ok "════════════════════════════════════════════"
ok "  $SPRINT_NAME задеплоен успешно!"
ok "  Коммит: ${NEW_COMMIT:0:7}"
ok "════════════════════════════════════════════"

log ""
log "История спринтов:"
for s in "$SPRINT_DIR"/sprint_*/; do
    [ -d "$s" ] || continue
    sname=$(basename "$s")
    commit=""
    status="⚠️  (отменён)"
    [ -f "$s/.deploy_commit" ] && commit=" $(cut -c1-7 < "$s/.deploy_commit")"
    [ -f "$s/.success" ] && status="✅"
    current=""
    [ "$(cat "$DEPLOY_DIR/.current_sprint" 2>/dev/null || true)" = "$sname" ] && current=" ← текущий"
    log "  $status $sname$commit$current"
done
