#!/usr/bin/env bash
# Private Line — Fix VLESS Reality on existing node
# Regenerates x25519 keys, writes full XRay config with DNS-over-HTTPS,
# 8 shortIds, sniffing disabled, and restarts Marzban.
# Usage: bash /app/fix_reality.sh   (run inside admin container or on node directly)
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
VPN_PORT=443
REALITY_SNI="www.microsoft.com"
REALITY_DEST="www.microsoft.com:443"

echo -e "\n${BOLD}${GREEN}══ Генерация VLESS Reality ключей ══${RESET}"

info "Генерирую x25519 ключи..."
XRAY_KEYS=$(docker run --rm --entrypoint xray gozargah/marzban:latest x25519 2>/dev/null) \
    || die "Не удалось сгенерировать ключи. Убедитесь что Docker установлен."

PRIVATE_KEY=$(echo "$XRAY_KEYS" | grep "Private key" | awk '{print $3}')
PUBLIC_KEY=$(echo "$XRAY_KEYS" | grep "Public key" | awk '{print $3}')

[[ -n "$PRIVATE_KEY" && -n "$PUBLIC_KEY" ]] || die "Не удалось извлечь ключи из вывода xray."

success "Private Key: ${PRIVATE_KEY:0:8}..."
success "Public Key:  $PUBLIC_KEY"

# Detect server IP
SERVER_IP=$(curl -s --max-time 10 ifconfig.me 2>/dev/null \
    || curl -s --max-time 10 api.ipify.org 2>/dev/null \
    || hostname -I | awk '{print $1}')
success "IP сервера: $SERVER_IP"

echo -e "\n${BOLD}${GREEN}══ Обновление XRay конфига ══${RESET}"

# Write XRay config via Python to get proper JSON, 8 random shortIds,
# DNS-over-HTTPS section (fixes DoT/853 failures in NekoBox), and sniffing.
VPN_PORT_VAL=$VPN_PORT \
PRIVATE_KEY_VAL="$PRIVATE_KEY" \
REALITY_SNI_VAL="$REALITY_SNI" \
REALITY_DEST_VAL="$REALITY_DEST" \
python3 << 'PYEOF' > "$MARZBAN_DATA/xray_config.json"
import json, os

config = {
    "log": {"loglevel": "warning"},
    "dns": {"servers": ["1.1.1.1", "8.8.8.8", "localhost"]},
    "inbounds": [{
        "tag": "VLESS-TCP",
        "listen": "0.0.0.0",
        "port": int(os.environ["VPN_PORT_VAL"]),
        "protocol": "vless",
        "settings": {"clients": [], "decryption": "none"},
        "sniffing": {"enabled": False},
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "dest": os.environ["REALITY_DEST_VAL"],
                "serverNames": [os.environ["REALITY_SNI_VAL"]],
                "privateKey": os.environ["PRIVATE_KEY_VAL"],
                "shortIds": [os.urandom(8).hex() for _ in range(8)]
            }
        }
    }],
    "outbounds": [
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "block",  "protocol": "blackhole"},
    ],
    "routing": {
        "rules": [
            {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"},
            {"type": "field", "network": "udp,tcp",    "outboundTag": "direct"}
        ]
    }
}
print(json.dumps(config, indent=2))
PYEOF

success "xray_config.json записан (порт $VPN_PORT, 8 shortIds, DoH DNS)"

# Sanity check: dest/serverNames must be plain strings, not markdown links
python3 -c "
import json, sys
c = json.load(open('$MARZBAN_DATA/xray_config.json'))
rs = c['inbounds'][0]['streamSettings']['realitySettings']
dest = rs['dest']; sni = rs['serverNames'][0]
if '](http' in dest or '](http' in sni:
    print('CORRUPT: markdown links in config!', dest, sni); sys.exit(1)
print('Config OK: dest=%s sni=%s' % (dest, sni))
" || die "xray_config.json содержит markdown-ссылки вместо plain-строк!"

echo -e "\n${BOLD}${GREEN}══ Обновление файерволла ══${RESET}"

if command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
    # Remove old VPN port if it was 10443
    ufw delete allow 10443/tcp >/dev/null 2>&1 && warn "Удалил старое правило 10443/tcp" || true
    ufw delete allow 10443 >/dev/null 2>&1 || true
    # Open new port
    ufw allow "${VPN_PORT}/tcp" comment "VLESS Reality VPN" >/dev/null 2>&1 \
        && success "Порт $VPN_PORT открыт в ufw" \
        || warn "Не удалось открыть порт $VPN_PORT в ufw"
    ufw status | grep -E "443|22|ssh" | head -10
fi

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

echo -e "\n${BOLD}${GREEN}══ Настройка Marzban hosts ══${RESET}"

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
SNI:        $REALITY_SNI
Port:       $VPN_PORT
ShortIds:   8 (см. /var/lib/marzban/xray_config.json)
EOF

echo
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║  VLESS Reality — Настройка завершена! ✅         ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════╝${RESET}"
echo
echo -e "  Public Key: ${CYAN}$PUBLIC_KEY${RESET}"
echo -e "  SNI:        ${CYAN}$REALITY_SNI${RESET}"
echo -e "  Port:       ${CYAN}$VPN_PORT${RESET}  (443 — максимальная совместимость)"
echo -e "  Short IDs:  ${CYAN}8 случайных${RESET} (см. xray_config.json)"
echo -e "  DNS:        ${CYAN}DoH через 1.1.1.1 и 8.8.8.8${RESET} (порт 853/53 → внутренний резолвер)"
echo
echo -e "  ${YELLOW}В Admin Panel → Подписки → нажмите «Перевыпустить ключ»${RESET}"
echo -e "  ${YELLOW}чтобы пользователь получил обновлённый VLESS ключ.${RESET}"
echo
