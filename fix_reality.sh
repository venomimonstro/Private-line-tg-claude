#!/usr/bin/env bash
# Private Line — Fix VLESS Reality on existing node
# Generates valid x25519 keys and restarts Marzban
# Usage: bash <(curl -fsSL http://YOUR_ADMIN/admin/api/fix-reality-script)
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET} $*"; }
success() { echo -e "${GREEN}[OK]${RESET}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET} $*"; }
die()     { echo -e "${RED}[ERR]${RESET}  $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Запустите от root."

MARZBAN_DIR="/opt/marzban"
MARZBAN_DATA="/var/lib/marzban"
VPN_PORT=10443
REALITY_SNI="www.microsoft.com"
REALITY_DEST="www.microsoft.com:443"

echo -e "\n${BOLD}${GREEN}══ Генерация VLESS Reality ключей ══${RESET}"

# Generate keys using docker image
info "Генерирую x25519 ключи..."
XRAY_KEYS=$(docker run --rm --entrypoint xray gozargah/marzban:latest x25519 2>/dev/null) \
    || die "Не удалось сгенерировать ключи. Убедитесь что Docker установлен."

PRIVATE_KEY=$(echo "$XRAY_KEYS" | grep "Private key" | awk '{print $3}')
PUBLIC_KEY=$(echo "$XRAY_KEYS" | grep "Public key" | awk '{print $3}')
SHORT_ID=$(openssl rand -hex 8)

[[ -n "$PRIVATE_KEY" && -n "$PUBLIC_KEY" ]] || die "Не удалось извлечь ключи из вывода xray."

success "Private Key: ${PRIVATE_KEY:0:8}..."
success "Public Key:  $PUBLIC_KEY"
success "Short ID:    $SHORT_ID"

# Detect server IP
SERVER_IP=$(curl -s --max-time 10 ifconfig.me 2>/dev/null \
    || curl -s --max-time 10 api.ipify.org 2>/dev/null \
    || hostname -I | awk '{print $1}')
success "IP сервера: $SERVER_IP"

echo -e "\n${BOLD}${GREEN}══ Обновление XRay конфига ══${RESET}"

cat > "$MARZBAN_DATA/xray_config.json" << XRAYEOF
{
  "log": {"loglevel": "warning"},
  "inbounds": [{
    "tag": "VLESS-TCP",
    "listen": "0.0.0.0",
    "port": ${VPN_PORT},
    "protocol": "vless",
    "settings": {"clients": [], "decryption": "none"},
    "streamSettings": {
      "network": "tcp",
      "security": "reality",
      "realitySettings": {
        "show": false,
        "dest": "${REALITY_DEST}",
        "serverNames": ["${REALITY_SNI}"],
        "privateKey": "${PRIVATE_KEY}",
        "shortIds": ["${SHORT_ID}"]
      }
    }
  }],
  "outbounds": [
    {"tag": "direct", "protocol": "freedom"},
    {"tag": "block", "protocol": "blackhole"}
  ],
  "routing": {
    "rules": [{"type": "field", "ip": ["geoip:private"], "outboundTag": "block"}]
  }
}
XRAYEOF

success "xray_config.json записан"

echo -e "\n${BOLD}${GREEN}══ Перезапуск Marzban ══${RESET}"

cd "$MARZBAN_DIR"
docker compose down 2>/dev/null || true
docker compose up -d
info "Жду запуска Marzban (до 60с)..."

# Read credentials from .env
MARZBAN_USER=$(grep SUDO_USERNAME "$MARZBAN_DIR/.env" | cut -d= -f2 | tr -d '"' | tr -d ' ')
MARZBAN_PASS=$(grep SUDO_PASSWORD "$MARZBAN_DIR/.env" | cut -d= -f2 | tr -d '"' | tr -d ' ')
API_URL="http://127.0.0.1:8000"

TOKEN=""
for i in $(seq 1 12); do
    sleep 5
    TOKEN=$(curl -sf -X POST "${API_URL}/api/admin/token" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "username=${MARZBAN_USER}&password=${MARZBAN_PASS}" \
        2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || echo "")
    if [[ -n "$TOKEN" ]]; then
        success "Marzban запущен (попытка ${i})"
        break
    fi
    info "Жду... ($((i*5))с)"
done

[[ -n "$TOKEN" ]] || die "Marzban не стартовал за 60с. Проверьте: docker compose logs"

echo -e "\n${BOLD}${GREEN}══ Настройка Marzban hosts (Reality параметры) ══${RESET}"

HOSTS_PAYLOAD=$(python3 - << PYEOF
import json
print(json.dumps({
    "VLESS-TCP": [{
        "remark": "Private Line VPN",
        "address": "${SERVER_IP}",
        "port": ${VPN_PORT},
        "sni": "${REALITY_SNI}",
        "host": "",
        "path": "",
        "security": "reality",
        "alpn": "",
        "fingerprint": "chrome",
        "allowinsecure": False,
        "is_disabled": False,
        "mux_enable": False,
        "fragment_setting": "",
        "noise_setting": "",
        "random_user_agent": False
    }]
}))
PYEOF
)

curl -sf -X PUT "${API_URL}/api/hosts" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$HOSTS_PAYLOAD" >/dev/null 2>&1 \
    && success "Marzban hosts настроены" \
    || warn "hosts API не сработал — ключи будут работать, но ссылки могут не включать pbk"

echo -e "\n${BOLD}${GREEN}══ Исправление существующих пользователей ══${RESET}"

# Get all users and fix their inbounds
USERS=$(curl -sf "${API_URL}/api/users?limit=1000" \
    -H "Authorization: Bearer $TOKEN" 2>/dev/null \
    | python3 -c "import sys,json; [print(u['username']) for u in json.load(sys.stdin).get('users',[])]" 2>/dev/null || echo "")

if [[ -n "$USERS" ]]; then
    while IFS= read -r username; do
        [[ -z "$username" ]] && continue
        RESULT=$(curl -sf -X PUT "${API_URL}/api/user/${username}" \
            -H "Authorization: Bearer $TOKEN" \
            -H "Content-Type: application/json" \
            -d '{"inbounds": {"vless": ["VLESS-TCP"]}, "proxies": {"vless": {"flow": "xtls-rprx-vision"}}, "status": "active"}' 2>/dev/null | python3 -c "
import sys,json
d=json.load(sys.stdin)
links=d.get('links',[])
print('\n'.join(links) if links else 'no links')" 2>/dev/null || echo "error")
        success "Пользователь $username → $RESULT"
    done <<< "$USERS"
else
    warn "Нет пользователей для обновления"
fi

# Save info
cat >> /root/marzban_node_info.txt << EOF

=== Reality Update $(date '+%Y-%m-%d %H:%M:%S') ===
Public Key: $PUBLIC_KEY
Short ID:   $SHORT_ID
SNI:        $REALITY_SNI
EOF

echo
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║  VLESS Reality — Настройка завершена! ✅         ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════╝${RESET}"
echo
echo -e "  Public Key: ${CYAN}$PUBLIC_KEY${RESET}"
echo -e "  Short ID:   ${CYAN}$SHORT_ID${RESET}"
echo -e "  SNI:        ${CYAN}$REALITY_SNI${RESET}"
echo -e "  Port:       ${CYAN}$VPN_PORT${RESET}"
echo
echo -e "  ${YELLOW}После этого в Admin Panel → Подписки → нажмите «Перевыпустить ключ»${RESET}"
echo -e "  ${YELLOW}чтобы пользователь получил новый Reality VLESS ключ.${RESET}"
echo
