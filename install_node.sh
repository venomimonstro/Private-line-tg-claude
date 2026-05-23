#!/usr/bin/env bash
# Private Line VPN Bot — VPN Node Installer
# Usage (copy from Admin Panel → Серверы):
#   ADMIN_URL=http://your-server NODE_TOKEN=xxx bash <(curl -fsSL http://your-server/admin/api/install-script)
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET} $*"; }
success() { echo -e "${GREEN}[OK]${RESET}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET} $*"; }
section() { echo -e "\n${BOLD}${BLUE}══ $* ══${RESET}"; }
die()     { echo -e "${RED}[ERR]${RESET}  $*" >&2; exit 1; }

# ─── Validate required env vars ───────────────────────────────────────────────
section "Проверка параметров"

[[ $EUID -eq 0 ]] || die "Запустите от root."

[[ -n "${ADMIN_URL:-}" ]] || die "Переменная ADMIN_URL не задана. Скопируйте команду из Admin Panel → Серверы."
[[ -n "${NODE_TOKEN:-}" ]] || die "Переменная NODE_TOKEN не задана. Скопируйте команду из Admin Panel → Серверы."

ADMIN_URL="${ADMIN_URL%/}"
success "Admin URL: $ADMIN_URL"
success "Токен задан"

# ─── OS check ─────────────────────────────────────────────────────────────────
OS_ID=$(grep -oP '(?<=^ID=).+' /etc/os-release | tr -d '"' || true)
OS_VER=$(grep -oP '(?<=^VERSION_ID=).+' /etc/os-release | tr -d '"' || true)
case "$OS_ID" in
    ubuntu|debian) success "ОС: $OS_ID $OS_VER" ;;
    *) die "Поддерживаются только Ubuntu и Debian. Обнаружена: $OS_ID" ;;
esac

# ─── Get server IP ────────────────────────────────────────────────────────────
SERVER_IP=$(curl -s --max-time 10 ifconfig.me 2>/dev/null \
    || curl -s --max-time 10 api.ipify.org 2>/dev/null \
    || hostname -I | awk '{print $1}')
success "IP сервера: $SERVER_IP"

# ─── Install dependencies ─────────────────────────────────────────────────────
section "Установка зависимостей"

apt-get update -qq

for pkg in curl wget ufw jq; do
    if ! command -v "$pkg" &>/dev/null; then
        apt-get install -y -qq "$pkg" >/dev/null 2>&1
        success "$pkg установлен"
    else
        success "$pkg уже установлен"
    fi
done

if ! command -v docker &>/dev/null; then
    info "Устанавливаю Docker..."
    curl -fsSL https://get.docker.com | bash
    systemctl enable docker --now >/dev/null 2>&1
    success "Docker установлен: $(docker --version)"
else
    success "Docker уже установлен: $(docker --version)"
fi

# ─── Auto-detect geo location ─────────────────────────────────────────────────
section "Определение расположения сервера"

info "Запрашиваю геоданные..."
GEO=$(curl -s --max-time 10 "http://ip-api.com/json/${SERVER_IP}?fields=country,countryCode,city" 2>/dev/null || echo "{}")
COUNTRY=$(echo "$GEO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('country','Unknown'))" 2>/dev/null || echo "Unknown")
COUNTRY_CODE=$(echo "$GEO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('countryCode','').lower())" 2>/dev/null || echo "")
CITY=$(echo "$GEO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('city',''))" 2>/dev/null || echo "")

# Build flag emoji
FLAG_EMOJI="🌐"
if [[ -n "$COUNTRY_CODE" ]]; then
    cc_upper=$(echo "$COUNTRY_CODE" | tr '[:lower:]' '[:upper:]')
    FLAG_EMOJI=""
    for (( i=0; i<${#cc_upper}; i++ )); do
        c="${cc_upper:$i:1}"
        code=$(printf '%d' "'$c")
        regional=$(( code - 65 + 127462 ))
        FLAG_EMOJI+=$(printf "\\U$(printf '%08X' $regional)")
    done
fi

# Auto-generate server name from country + last IP octet
LAST_OCTET=$(echo "$SERVER_IP" | grep -oP '\d+$' || echo "1")
SERVER_NAME="${COUNTRY// /-}-${LAST_OCTET}"

success "Страна: $COUNTRY (${COUNTRY_CODE:-?}) $FLAG_EMOJI"
success "Город:  ${CITY:-неизвестен}"
success "Имя ноды: $SERVER_NAME"

# ─── Install Outline VPN ──────────────────────────────────────────────────────
section "Установка Outline VPN"

info "Запускаю установщик Outline (2–5 минут)..."
OUTLINE_LOG=$(mktemp)

bash -c "$(wget -qO- https://raw.githubusercontent.com/Jigsaw-Code/outline-server/master/src/server_manager/install_scripts/install_server.sh)" \
    2>&1 | tee "$OUTLINE_LOG"

echo

# ─── Extract Outline API credentials ─────────────────────────────────────────
section "Извлечение данных подключения"

OUTLINE_JSON=$(grep -oP '\{[^{}]*"apiUrl"[^{}]*"certSha256"[^{}]*\}' "$OUTLINE_LOG" \
    || grep -oP '\{[^{}]*"certSha256"[^{}]*"apiUrl"[^{}]*\}' "$OUTLINE_LOG" \
    || true)

if [[ -z "$OUTLINE_JSON" ]]; then
    cat "$OUTLINE_LOG"
    rm -f "$OUTLINE_LOG"
    die "Не удалось извлечь конфигурацию Outline. Смотрите вывод выше."
fi

API_URL=$(echo "$OUTLINE_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('apiUrl',''))" 2>/dev/null || true)
CERT_SHA256=$(echo "$OUTLINE_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('certSha256',''))" 2>/dev/null || true)

[[ -n "$API_URL" ]] || die "Не удалось получить API URL от Outline."
success "Outline API URL получен"
rm -f "$OUTLINE_LOG"

# ─── Firewall ─────────────────────────────────────────────────────────────────
section "Настройка файерволла"

MGMT_PORT=$(echo "$API_URL" | grep -oP ':\K[0-9]+(?=/)' || echo "")

ufw --force reset >/dev/null 2>&1
ufw default deny incoming >/dev/null 2>&1
ufw default allow outgoing >/dev/null 2>&1
ufw allow 22/tcp comment "SSH" >/dev/null 2>&1

[[ -n "$MGMT_PORT" ]] && ufw allow "${MGMT_PORT}/tcp" comment "Outline management" >/dev/null 2>&1

OUTLINE_ACCESS_PORT=$(docker ps --format '{{.Ports}}' 2>/dev/null \
    | grep -oP '0\.0\.0\.0:\K[0-9]+(?=->)' | grep -v "${MGMT_PORT:-__}" | head -1 || true)
if [[ -n "$OUTLINE_ACCESS_PORT" ]]; then
    ufw allow "${OUTLINE_ACCESS_PORT}/tcp" >/dev/null 2>&1
    ufw allow "${OUTLINE_ACCESS_PORT}/udp" >/dev/null 2>&1
    success "Открыты порты: 22, $MGMT_PORT (mgmt), $OUTLINE_ACCESS_PORT (vpn)"
else
    ufw allow 443/tcp >/dev/null 2>&1
    ufw allow 443/udp >/dev/null 2>&1
    success "Открыты порты: 22, ${MGMT_PORT:-443}, 443"
fi

ufw --force enable >/dev/null 2>&1

# ─── Auto-register in admin panel ─────────────────────────────────────────────
section "Регистрация ноды в Admin Panel"

info "Подключаюсь к $ADMIN_URL ..."

PAYLOAD=$(python3 - <<PYEOF
import json
print(json.dumps({
    "name":         "${SERVER_NAME}",
    "country":      "${COUNTRY}",
    "city":         "${CITY}",
    "country_code": "${COUNTRY_CODE}",
    "flag_emoji":   "${FLAG_EMOJI}",
    "provider":     "custom",
    "api_url":      "${API_URL}",
    "cert_sha256":  "${CERT_SHA256}",
    "max_users":    100
}))
PYEOF
)

HTTP_RESPONSE=$(curl -s -w "\n%{http_code}" \
    -X POST "${ADMIN_URL}/admin/api/register-node" \
    -H "Content-Type: application/json" \
    -H "X-Node-Token: ${NODE_TOKEN}" \
    -d "$PAYLOAD" \
    --max-time 30)

HTTP_CODE=$(echo "$HTTP_RESPONSE" | tail -1)
HTTP_BODY=$(echo "$HTTP_RESPONSE" | head -n -1)

if [[ "$HTTP_CODE" == "200" ]]; then
    REG_MSG=$(echo "$HTTP_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('message',''))" 2>/dev/null || echo "$HTTP_BODY")
    success "Нода зарегистрирована!"
    echo -e "  ${GREEN}${REG_MSG}${RESET}"
    REGISTERED=true
else
    warn "Авторегистрация не удалась (HTTP ${HTTP_CODE}): $HTTP_BODY"
    warn "Добавьте вручную: Admin Panel → Серверы → API URL: $API_URL"
    REGISTERED=false
fi

# ─── Save info ────────────────────────────────────────────────────────────────
cat > /root/outline_node_info.txt <<EOF
=== Private Line VPN Node ===
Установлено: $(date '+%Y-%m-%d %H:%M:%S')
Имя:    $SERVER_NAME
Страна: $COUNTRY ($COUNTRY_CODE)
Город:  ${CITY:-—}
IP:     $SERVER_IP
API URL: $API_URL
EOF

# ─── Summary ──────────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║  Private Line Node — Установка завершена!   ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════╝${RESET}"
echo
echo -e "  Нода:    ${CYAN}$SERVER_NAME${RESET}"
echo -e "  IP:      ${CYAN}$SERVER_IP${RESET}"
echo -e "  Страна:  ${CYAN}$COUNTRY $FLAG_EMOJI${RESET}"
if [[ "${REGISTERED:-false}" == "true" ]]; then
    echo -e "  Статус:  ${GREEN}✅ Зарегистрирована в Admin Panel${RESET}"
else
    echo -e "  Статус:  ${YELLOW}⚠️  Требуется ручная регистрация${RESET}"
    echo -e "  API URL: ${YELLOW}$API_URL${RESET}"
fi
echo
docker ps --filter "name=shadowbox" --format "  Outline: {{.Status}}" 2>/dev/null || true
echo
