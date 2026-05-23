#!/usr/bin/env bash
# Private Line VPN Bot — VPN Node Installer (Marzban + XRay VLESS)
# Usage (copy from Admin Panel → VPN Серверы):
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
[[ -n "${ADMIN_URL:-}" ]] || die "ADMIN_URL не задан. Скопируйте команду из Admin Panel → VPN Серверы."
[[ -n "${NODE_TOKEN:-}" ]] || die "NODE_TOKEN не задан. Скопируйте команду из Admin Panel → VPN Серверы."

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

for pkg in curl wget ufw jq python3; do
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

GEO=$(curl -s --max-time 10 "http://ip-api.com/json/${SERVER_IP}?fields=country,countryCode,city" 2>/dev/null || echo "{}")
COUNTRY=$(echo "$GEO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('country','Unknown'))" 2>/dev/null || echo "Unknown")
COUNTRY_CODE=$(echo "$GEO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('countryCode','').lower())" 2>/dev/null || echo "")
CITY=$(echo "$GEO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('city',''))" 2>/dev/null || echo "")

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

LAST_OCTET=$(echo "$SERVER_IP" | grep -oP '\d+$' || echo "1")
SERVER_NAME="${COUNTRY// /-}-${LAST_OCTET}"

success "Страна: $COUNTRY (${COUNTRY_CODE:-?}) $FLAG_EMOJI"
success "Имя ноды: $SERVER_NAME"

# ─── Install Marzban (XRay + VLESS panel) ────────────────────────────────────
section "Установка Marzban (XRay VLESS)"

MARZBAN_DIR="/opt/marzban"
MARZBAN_DATA="/var/lib/marzban"
MARZBAN_PORT=8000
VPN_PORT=10443
MARZBAN_USER="admin"
MARZBAN_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")

mkdir -p "$MARZBAN_DIR" "$MARZBAN_DATA"

# XRay config with VLESS TCP inbound
cat > "$MARZBAN_DATA/xray_config.json" << XRAYEOF
{
  "log": {"loglevel": "warning"},
  "inbounds": [{
    "tag": "VLESS-TCP",
    "listen": "0.0.0.0",
    "port": ${VPN_PORT},
    "protocol": "vless",
    "settings": {"clients": [], "decryption": "none"},
    "streamSettings": {"network": "tcp"}
  }],
  "outbounds": [
    {"tag": "direct", "protocol": "freedom"},
    {"tag": "block", "protocol": "blackhole"}
  ],
  "routing": {
    "rules": [
      {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"}
    ]
  }
}
XRAYEOF

# Marzban docker-compose
cat > "$MARZBAN_DIR/docker-compose.yml" << 'DCEOF'
version: "3"
services:
  marzban:
    image: gozargah/marzban:latest
    restart: always
    env_file: /opt/marzban/.env
    network_mode: host
    volumes:
      - /var/lib/marzban:/var/lib/marzban
DCEOF

# Marzban env
cat > "$MARZBAN_DIR/.env" << EOF
UVICORN_HOST = "0.0.0.0"
UVICORN_PORT = ${MARZBAN_PORT}
SUDO_USERNAME = "${MARZBAN_USER}"
SUDO_PASSWORD = "${MARZBAN_PASS}"
XRAY_JSON = "/var/lib/marzban/xray_config.json"
EOF

info "Запускаю Marzban (первый запуск скачает XRay ~60МБ)..."
cd "$MARZBAN_DIR" && docker compose up -d

# Wait for Marzban API to become ready (up to 120s)
info "Жду готовности API..."
for i in $(seq 1 24); do
    if curl -sf "http://127.0.0.1:${MARZBAN_PORT}/docs" >/dev/null 2>&1; then
        success "Marzban API готов (${i})"
        break
    fi
    sleep 5
    if [[ $i -eq 24 ]]; then
        warn "API не ответил за 120с — проверьте: docker compose -f /opt/marzban/docker-compose.yml logs"
    fi
done

# Verify credentials work
API_URL="http://127.0.0.1:${MARZBAN_PORT}"
TOKEN=$(curl -sf -X POST "${API_URL}/api/admin/token" \
    -d "username=${MARZBAN_USER}&password=${MARZBAN_PASS}" \
    2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || echo "")

if [[ -n "$TOKEN" ]]; then
    success "Аутентификация Marzban прошла успешно"
else
    warn "Не удалось получить токен Marzban. Продолжаю регистрацию..."
fi

# ─── Firewall ─────────────────────────────────────────────────────────────────
section "Настройка файерволла"

ufw --force reset >/dev/null 2>&1
ufw default deny incoming >/dev/null 2>&1
ufw default allow outgoing >/dev/null 2>&1
ufw allow 22/tcp comment "SSH" >/dev/null 2>&1
ufw allow "${VPN_PORT}/tcp" comment "VLESS VPN" >/dev/null 2>&1
ufw allow "${MARZBAN_PORT}/tcp" comment "Marzban API" >/dev/null 2>&1
ufw --force enable >/dev/null 2>&1
success "Открыты порты: 22 (SSH), $VPN_PORT (VLESS), $MARZBAN_PORT (API)"

# ─── Register node in admin panel ─────────────────────────────────────────────
section "Регистрация ноды в Admin Panel"

# Use public IP for API URL (not localhost)
PUBLIC_API_URL="http://${SERVER_IP}:${MARZBAN_PORT}"
info "Регистрирую: $PUBLIC_API_URL"

PAYLOAD=$(python3 - << PYEOF
import json
print(json.dumps({
    "name":              "${SERVER_NAME}",
    "country":           "${COUNTRY}",
    "city":              "${CITY}",
    "country_code":      "${COUNTRY_CODE}",
    "flag_emoji":        "${FLAG_EMOJI}",
    "provider":          "marzban",
    "api_url":           "${PUBLIC_API_URL}",
    "marzban_username":  "${MARZBAN_USER}",
    "marzban_password":  "${MARZBAN_PASS}",
    "max_users":         200
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
    warn "Добавьте вручную через Admin Panel → VPN Серверы"
    REGISTERED=false
fi

# ─── Save info ────────────────────────────────────────────────────────────────
INFO_FILE="/root/marzban_node_info.txt"
cat > "$INFO_FILE" << EOF
=== Private Line VPN Node (Marzban) ===
Установлено: $(date '+%Y-%m-%d %H:%M:%S')
Имя:     $SERVER_NAME
Страна:  $COUNTRY ($COUNTRY_CODE)
IP:      $SERVER_IP

=== Marzban API ===
URL:      $PUBLIC_API_URL
Логин:    $MARZBAN_USER
Пароль:   $MARZBAN_PASS
Веб-UI:   $PUBLIC_API_URL (откройте в браузере)

=== Порты ===
VLESS VPN:   $VPN_PORT/tcp
Marzban API: $MARZBAN_PORT/tcp
EOF

success "Конфигурация сохранена: $INFO_FILE"

# ─── Summary ──────────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║   Private Line Node — Установка завершена!   ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════╝${RESET}"
echo
echo -e "  Нода:    ${CYAN}$SERVER_NAME${RESET}  |  IP: ${CYAN}$SERVER_IP${RESET}"
echo -e "  Страна:  ${CYAN}$COUNTRY $FLAG_EMOJI${RESET}"
echo -e "  VLESS:   порт ${CYAN}$VPN_PORT${RESET}"
if [[ "${REGISTERED:-false}" == "true" ]]; then
    echo -e "  Статус:  ${GREEN}✅ Зарегистрирована в Admin Panel${RESET}"
else
    echo -e "  Статус:  ${YELLOW}⚠️  Требуется ручная регистрация${RESET}"
    echo -e "  API URL: ${YELLOW}$PUBLIC_API_URL${RESET}"
    echo -e "  Логин:   ${YELLOW}$MARZBAN_USER${RESET} / Пароль: ${YELLOW}$MARZBAN_PASS${RESET}"
fi
echo
echo -e "${BOLD}📊 Статус Marzban:${RESET}"
docker compose -f "$MARZBAN_DIR/docker-compose.yml" ps 2>/dev/null || true
echo
