#!/usr/bin/env bash
# Private Line VPN Bot — VPN Node Installer (Marzban + XRay VLESS Reality)
# Usage (copy from Admin Panel → VPN Серверы):
#   ADMIN_URL=https://control-plane ADMIN_PIN=sha256//... NODE_TOKEN=xxx bash <(curl --insecure --pinnedpubkey "$ADMIN_PIN" -fsSL https://control-plane/admin/api/install-script)
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
[[ ! -f /opt/private-line/docker-compose.yml ]] || die "Это основной control-plane сервер. VPN-ноды устанавливаются только на отдельные VPS."
[[ -n "${ADMIN_URL:-}" ]] || die "ADMIN_URL не задан. Скопируйте команду из Admin Panel → VPN Серверы."
[[ -n "${ADMIN_PIN:-}" ]] || die "ADMIN_PIN не задан. Используйте полную команду из Admin Panel."
[[ -n "${NODE_TOKEN:-}" ]] || die "NODE_TOKEN не задан. Скопируйте команду из Admin Panel → VPN Серверы."

ADMIN_URL="${ADMIN_URL%/}"
[[ "$ADMIN_URL" == https://* ]] || die "Регистрация VPN-ноды разрешена только через HTTPS."
[[ "$ADMIN_PIN" =~ ^sha256//[A-Za-z0-9+/]{43}=$ ]] || die "Некорректный ADMIN_PIN."
success "Admin URL: $ADMIN_URL"
success "TLS pin задан"
success "Токен задан"

# Marzban API on a VPN node is reachable only from the control-plane server.
# Derive it from ADMIN_URL unless explicitly supplied.
if [[ -z "${MAIN_SERVER_IP:-}" ]]; then
    MAIN_SERVER_IP="$(ADMIN_URL="$ADMIN_URL" python3 - <<'PY'
import ipaddress, os
from urllib.parse import urlsplit
host = urlsplit(os.environ["ADMIN_URL"]).hostname or ""
try:
    ip = ipaddress.IPv4Address(host)
    print(ip if ip.is_global else "")
except ValueError:
    print("")
PY
)"
fi
[[ -n "$MAIN_SERVER_IP" ]] || die "Не удалось определить MAIN_SERVER_IP из ADMIN_URL. Укажите MAIN_SERVER_IP вручную."

# ─── OS check ─────────────────────────────────────────────────────────────────
OS_ID=$(grep -oP '(?<=^ID=).+' /etc/os-release | tr -d '"' || true)
OS_VER=$(grep -oP '(?<=^VERSION_ID=).+' /etc/os-release | tr -d '"' || true)
case "$OS_ID" in
    ubuntu|debian) success "ОС: $OS_ID $OS_VER" ;;
    *) die "Поддерживаются только Ubuntu и Debian. Обнаружена: $OS_ID" ;;
esac

# ─── Get public IPv4 ────────────────────────────────────────────────────────
# VLESS hosts and Marzban API must use a routable address. Do not silently
# accept IPv6 from dual-stack IP echo endpoints: unbracketed IPv6 breaks URLs.
# NODE_PUBLIC_IP lets operators override discovery (useful for NAT setups).
SERVER_IP="${NODE_PUBLIC_IP:-}"
if [[ -z "$SERVER_IP" ]]; then
    SERVER_IP=$(curl -4 -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)
fi
if [[ -z "$SERVER_IP" ]]; then
    SERVER_IP=$(curl -4 -fsS --max-time 10 https://ifconfig.me/ip 2>/dev/null || true)
fi
if ! SERVER_IP="$SERVER_IP" python3 -c '
import ipaddress, os, sys
try:
    ip = ipaddress.IPv4Address(os.environ["SERVER_IP"].strip())
    if not ip.is_global:
        sys.exit(1)
except ValueError:
    sys.exit(1)
' ; then
    die "Не удалось определить публичный IPv4. Укажите NODE_PUBLIC_IP=ваш_IP перед запуском."
fi
success "Публичный IPv4 сервера: $SERVER_IP"

# ─── Install dependencies ─────────────────────────────────────────────────────
section "Установка зависимостей"

apt-get update -qq

for pkg in curl wget ufw jq python3 openssl; do
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

# ─── Install Marzban (XRay + VLESS Reality) ───────────────────────────────────
section "Установка Marzban (XRay VLESS Reality)"

# Never reinstall over an existing node. Reinstalling would rotate Marzban
# credentials and Reality keys and invalidate every issued client profile.
if [[ -f /opt/marzban/.env || -f /var/lib/marzban/xray_config.json ]]; then
    die "Существующая VPN-нода обнаружена. Повторная установка запрещена; используйте безопасное обновление Xray и control-plane reconcile."
fi

MARZBAN_DIR="/opt/marzban"
MARZBAN_DATA="/var/lib/marzban"
MARZBAN_PORT=2096
VPN_PORT=443
PROXY_PORT=2096
MARZBAN_USER="admin"
MARZBAN_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
# Default target follows XTLS REALITY guidance: TLS 1.3 + H2 and stable
# handshake shape. www.microsoft.com is intentionally avoided because its
# certificate record has triggered REALITY >8192-byte handshake failures.
REALITY_TARGET_HOST="${REALITY_TARGET_HOST:-dl.google.com}"
REALITY_SNI="$REALITY_TARGET_HOST"
REALITY_DEST="${REALITY_TARGET_HOST}:443"

mkdir -p "$MARZBAN_DIR" "$MARZBAN_DATA"

# Reproducible runtime: pin Marzban v0.8.4 and Xray 26.6.27 independently.
# Current REALITY clients are not reliably compatible with Marzban's old
# bundled core. A tiny Marzban operations.py override adds bounded gRPC
# hot-add/hot-remove retries; real E2E still decides whether this node is usable.
case "$(uname -m)" in
    x86_64)
        MARZBAN_IMAGE="ghcr.io/gozargah/marzban:v0.8.4@sha256:953700b0b91b35dcd5165072fc7029fc3c5bd6ed10d644f80d7cb9a84a9a27f1"
        ;;
    aarch64|arm64)
        MARZBAN_IMAGE="ghcr.io/gozargah/marzban:v0.8.4@sha256:886f11d701005a80d381d4e4d659ae376d2fff55470578f115b518687d86453e"
        ;;
    *)
        die "Неподдерживаемая архитектура Marzban: $(uname -m)"
        ;;
esac
XRAY_BIN="/var/lib/marzban/xray-core/26.6.27/xray"

info "Загружаю зафиксированный официальный Marzban runtime..."
docker pull "$MARZBAN_IMAGE" >/dev/null
MARZBAN_PIN="$MARZBAN_IMAGE"
docker image inspect "$MARZBAN_IMAGE" >/dev/null 2>&1 \
    || die "Digest-pinned Marzban image не загружен."

# Reality target must be a stable TLS 1.3 + HTTP/2 endpoint.
TARGET_TLS_LOG="$(mktemp)"
if ! timeout 15 openssl s_client -connect "$REALITY_DEST" -servername "$REALITY_SNI" \
    -tls1_3 -alpn h2 </dev/null >"$TARGET_TLS_LOG" 2>&1; then
    rm -f "$TARGET_TLS_LOG"
    die "Reality target $REALITY_DEST недоступен по TLS 1.3 с этой ноды."
fi
grep -q 'ALPN protocol: h2' "$TARGET_TLS_LOG" || {
    rm -f "$TARGET_TLS_LOG"
    die "Reality target $REALITY_DEST не поддерживает HTTP/2 ALPN."
}
rm -f "$TARGET_TLS_LOG"

# Temporary plain config — Reality keys generated after container starts
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
      {"type": "field", "ip": [
                "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                "127.0.0.0/8", "169.254.0.0/16",
                "::1/128", "fc00::/7", "fe80::/10"
            ], "outboundTag": "block"},
      {"type": "field", "network": "udp,tcp", "outboundTag": "direct"}
    ]
  }
}
XRAYEOF

# Marzban docker-compose (digest-pinned; no implicit upgrades on restart)
cat > "$MARZBAN_DIR/docker-compose.yml" << DCEOF
services:
  marzban:
    image: $MARZBAN_PIN
    restart: unless-stopped
    env_file: /opt/marzban/.env
    network_mode: host
    volumes:
      - /var/lib/marzban:/var/lib/marzban
DCEOF

# Marzban API listens directly on 2096. UFW exposes it only to control-plane.
# No socat/reverse-proxy process is required on a VPN node.
cat > "$MARZBAN_DIR/.env" << EOF
UVICORN_HOST=0.0.0.0
UVICORN_PORT=${MARZBAN_PORT}
SUDO_USERNAME=${MARZBAN_USER}
SUDO_PASSWORD=${MARZBAN_PASS}
XRAY_JSON=/var/lib/marzban/xray_config.json
XRAY_EXECUTABLE_PATH=/var/lib/marzban/xray-core/26.6.27/xray
XRAY_ASSETS_PATH=/usr/local/share/xray
EOF

chmod 600 "$MARZBAN_DIR/.env"

# Fetch the exact runtime preparer through the already pinned control-plane
# connection. It installs Xray 26.6.27 and a read-only Marzban hot-add retry
# override before the first Marzban process starts.
RUNTIME_PREPARER="$(mktemp)"
trap 'rm -f "$RUNTIME_PREPARER"' EXIT
curl --insecure --pinnedpubkey "$ADMIN_PIN" -fsSL \
    -H "X-Node-Token: ${NODE_TOKEN}" \
    "${ADMIN_URL}/admin/api/marzban-runtime-script" \
    -o "$RUNTIME_PREPARER" \
    || die "Не удалось получить runtime preparer с control-plane."
bash -n "$RUNTIME_PREPARER" || die "Runtime preparer имеет некорректный shell syntax."
MARZBAN_IMAGE="$MARZBAN_IMAGE" MARZBAN_DIR="$MARZBAN_DIR" MARZBAN_DATA="$MARZBAN_DATA" \
    bash "$RUNTIME_PREPARER" \
    || die "Не удалось подготовить Xray/Marzban runtime."
rm -f "$RUNTIME_PREPARER"
trap - EXIT

# Close management API before the first process ever binds it.
# UFW rules are staged first, then enabled, so TCP/2096 is reachable only
# from the control-plane IP from the first second of service life.
ufw allow 22/tcp comment "SSH" >/dev/null 2>&1 || true
ufw allow "${VPN_PORT}/tcp" comment "VLESS Reality VPN" >/dev/null 2>&1 || true
ufw delete allow "${PROXY_PORT}/tcp" >/dev/null 2>&1 || true
ufw allow from "$MAIN_SERVER_IP" to any port "$PROXY_PORT" proto tcp \
    comment "Marzban API control plane" >/dev/null 2>&1 \
    || die "Не удалось ограничить Marzban API по IP основного сервера."
ufw --force enable >/dev/null 2>&1 || die "Не удалось включить UFW."

info "Запускаю pinned Marzban с Xray 26.6.27..."
cd "$MARZBAN_DIR" && docker compose up -d

# Wait for Marzban API to become ready (up to 120s)
info "Жду готовности API..."
API_URL="http://127.0.0.1:${MARZBAN_PORT}"
for i in $(seq 1 24); do
    if curl -sf -X POST "${API_URL}/api/admin/token" \
        -d "username=${MARZBAN_USER}&password=${MARZBAN_PASS}" \
        >/dev/null 2>&1; then
        success "Marzban API готов (попытка ${i})"
        break
    fi
    sleep 5
    if [[ $i -eq 24 ]]; then
        die "Marzban API не ответил за 120с. Установка остановлена."
    fi
done

SERVER_XRAY_VERSION="$(docker compose exec -T marzban "$XRAY_BIN" version 2>/dev/null | head -n1 || true)"
[[ "$SERVER_XRAY_VERSION" == Xray\ 26.6.27* ]] || die "Marzban не запустил pinned Xray 26.6.27."
docker compose exec -T marzban grep -q "PRIVATE_LINE_XRAY_HOTADD_RETRY_V1" \
    /code/app/xray/operations.py \
    || die "Marzban hot-add retry override не активирован."
success "Runtime: $SERVER_XRAY_VERSION + Marzban hot-add retry override"

# ─── Generate VLESS Reality keys ──────────────────────────────────────────────
section "Генерация VLESS Reality ключей"

XRAY_KEYS="$("$XRAY_BIN" x25519 2>/dev/null || true)"
[[ -n "$XRAY_KEYS" ]] || die "Xray 26.6.27 не смог сгенерировать Reality keypair."

# Xray outputs vary by release:
# legacy: "Private key:" / "Public key:"
# recent: "PrivateKey:" / "Password:" / "Password (PublicKey):"
# Match the label, never mistake Hash32 for the public key.
PRIVATE_KEY=$(printf '%s\n' "$XRAY_KEYS" | awk -F ':[[:space:]]*' 'tolower($1) ~ /^private ?key$/ { print $2; exit }')
PUBLIC_KEY=$(printf '%s\n' "$XRAY_KEYS" | awk -F ':[[:space:]]*' 'tolower($1) ~ /^(public ?key|password( \(publickey\))?)$/ { print $2; exit }')

[[ "$PRIVATE_KEY" =~ ^[A-Za-z0-9_-]{43,44}$ && "$PUBLIC_KEY" =~ ^[A-Za-z0-9_-]{43,44}$ ]] || die "Не удалось извлечь корректные Reality ключи. Проверьте вывод xray x25519."

success "Reality Public Key: $PUBLIC_KEY"

# Write final XRay config with Reality via Python:
#  - one stable random shortId;
#  - explicit DNS for deterministic node egress.
VPN_PORT_VAL=$VPN_PORT \
PRIVATE_KEY_VAL="$PRIVATE_KEY" \
PUBLIC_KEY_VAL="$PUBLIC_KEY" \
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
            "tcpSettings": {},
            "security": "reality",
            "realitySettings": {
                "show": False,
                "dest": os.environ["REALITY_DEST_VAL"],
                "xver": 0,
                "serverNames": [os.environ["REALITY_SNI_VAL"]],
                "privateKey": os.environ["PRIVATE_KEY_VAL"],
                # Marzban 0.8.x expects publicKey metadata when its bundled
                # x25519 output parser cannot understand newer Xray labels.
                "publicKey": os.environ["PUBLIC_KEY_VAL"],
                "minClientVer": "",
                "maxClientVer": "",
                "maxTimeDiff": 0,
                "shortIds": [os.urandom(8).hex()]
            }
        }
    }],
    "outbounds": [
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "block",  "protocol": "blackhole"},
    ],
    "routing": {
        "rules": [
            {"type": "field", "ip": [
                "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                "127.0.0.0/8", "169.254.0.0/16",
                "::1/128", "fc00::/7", "fe80::/10"
            ], "outboundTag": "block"},
            {"type": "field", "network": "udp,tcp",    "outboundTag": "direct"}
        ]
    }
}
print(json.dumps(config, indent=2))
PYEOF

success "XRay конфиг с Reality записан (порт $VPN_PORT, один стабильный shortId)"

"$XRAY_BIN" run -test -config "$MARZBAN_DATA/xray_config.json" </dev/null >/dev/null \
    || die "Xray 26.6.27 отклонил финальный VLESS/REALITY конфиг."

# Restart Marzban to apply Reality config
info "Перезапускаю Marzban с Reality конфигом..."
cd "$MARZBAN_DIR" && docker compose restart
sleep 10

# Wait for API again
API_READY=false
for i in $(seq 1 12); do
    if curl -sf -X POST "${API_URL}/api/admin/token" \
        -d "username=${MARZBAN_USER}&password=${MARZBAN_PASS}" \
        >/dev/null 2>&1; then
        success "Marzban API готов после перезапуска"
        API_READY=true
        break
    fi
    sleep 5
done
[[ "$API_READY" == true ]] || die "Marzban API не восстановился после применения Reality-конфигурации."

# Get token
TOKEN=$(curl -sf -X POST "${API_URL}/api/admin/token" \
    -d "username=${MARZBAN_USER}&password=${MARZBAN_PASS}" \
    2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || echo "")

[[ -n "$TOKEN" ]] || die "Не удалось получить Marzban admin token."

# Configure Marzban hosts so links include Reality params
if [[ -n "$TOKEN" ]]; then
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
        # Marzban hosts inherit transport security from the Xray inbound.
        "security": "inbound_default",
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
        || die "Marzban /api/hosts не сохранил публичный VPN endpoint."
fi

# Bootstrap admin remains in the root-only .env file. This is the standard
# Marzban bootstrap path and avoids an unnecessary credential migration/restart.
success "Marzban API admin готов; credentials сохранены в /opt/marzban/.env (0600)."

# ─── Firewall ─────────────────────────────────────────────────────────────────
section "Настройка файерволла"

# Preserve all existing firewall rules and defaults; never reset UFW over SSH.
ufw allow 22/tcp comment "SSH" >/dev/null 2>&1
ufw allow "${VPN_PORT}/tcp" comment "VLESS Reality VPN" >/dev/null 2>&1
# Remove any legacy broad API allow rule, then permit only the control plane.
ufw delete allow "${PROXY_PORT}/tcp" >/dev/null 2>&1 || true
ufw allow from "$MAIN_SERVER_IP" to any port "$PROXY_PORT" proto tcp comment "Marzban API (control plane only)" >/dev/null 2>&1
success "API порт $PROXY_PORT доступен только с control-plane $MAIN_SERVER_IP"
ufw --force enable >/dev/null 2>&1
success "Открыты SSH 22 и VLESS Reality $VPN_PORT"

# Marzban listens directly on the firewall-restricted API port; no socat layer.
success "Marzban API напрямую слушает $PROXY_PORT; отдельный proxy-сервис не используется."

# ─── Register node in admin panel ─────────────────────────────────────────────
section "Регистрация ноды в Admin Panel"

PUBLIC_API_URL="http://${SERVER_IP}:${PROXY_PORT}"
info "Регистрирую: $PUBLIC_API_URL"

PAYLOAD=$(python3 - << PYEOF
import json
print(json.dumps({
    "name":             "${SERVER_NAME}",
    "country":          "${COUNTRY}",
    "city":             "${CITY}",
    "country_code":     "${COUNTRY_CODE}",
    "flag_emoji":       "${FLAG_EMOJI}",
    "provider":         "marzban",
    "api_url":          "${PUBLIC_API_URL}",
    "marzban_username": "${MARZBAN_USER}",
    "marzban_password": "${MARZBAN_PASS}",
    "max_users":        200
}))
PYEOF
)

HTTP_RESPONSE=$(curl --insecure --pinnedpubkey "$ADMIN_PIN" -sS -w "\n%{http_code}" \
    -X POST "${ADMIN_URL}/admin/api/register-node" \
    -H "Content-Type: application/json" \
    -H "X-Node-Token: ${NODE_TOKEN}" \
    -d "$PAYLOAD" \
    --max-time 30 || printf '\n000')

HTTP_CODE=$(echo "$HTTP_RESPONSE" | tail -1)
HTTP_BODY=$(echo "$HTTP_RESPONSE" | head -n -1)

if [[ "$HTTP_CODE" == "200" ]]; then
    REG_MSG=$(echo "$HTTP_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('message',''))" 2>/dev/null || echo "$HTTP_BODY")
    success "Нода зарегистрирована как STAGING (ещё не выдаётся клиентам)."
    echo -e "  ${GREEN}${REG_MSG}${RESET}"
    REGISTERED=true
else
    die "Control-plane не подтвердил VPN-ноду (HTTP ${HTTP_CODE}). Нода не опубликована. Ответ: ${HTTP_BODY:0:300}"
fi

# ─── Save info ────────────────────────────────────────────────────────────────
INFO_FILE="/root/marzban_node_info.txt"
cat > "$INFO_FILE" << EOF
=== Private Line VPN Node (Marzban + VLESS Reality) ===
Установлено: $(date '+%Y-%m-%d %H:%M:%S')
Имя:     $SERVER_NAME
Страна:  $COUNTRY ($COUNTRY_CODE)
IP:      $SERVER_IP

=== VLESS Reality ===
SNI (маскировка): $REALITY_SNI
Public Key:  $PUBLIC_KEY
ShortIds:    1 стабильный (см. /var/lib/marzban/xray_config.json)
VPN Port:    $VPN_PORT (443 — максимальная совместимость)

=== Marzban API ===
URL:      $PUBLIC_API_URL
Логин:    $MARZBAN_USER
Пароль:   $MARZBAN_PASS
EOF

chmod 600 "$INFO_FILE"
success "Конфигурация сохранена: $INFO_FILE"

# ─── Summary ──────────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║  Private Line Node — Установка завершена! ✅     ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════╝${RESET}"
echo
echo -e "  Нода:      ${CYAN}$SERVER_NAME${RESET}  |  IP: ${CYAN}$SERVER_IP${RESET}"
echo -e "  Страна:    ${CYAN}$COUNTRY $FLAG_EMOJI${RESET}"
echo -e "  Протокол:  ${GREEN}VLESS Reality${RESET} (маскировка под ${REALITY_SNI})"
echo -e "  VPN Порт:  ${CYAN}$VPN_PORT${RESET}"
echo -e "  Public Key: ${CYAN}$PUBLIC_KEY${RESET}"
if [[ "${REGISTERED:-false}" == "true" ]]; then
    echo -e "  Статус:    ${YELLOW}STAGING — нужна E2E проверка с control-plane${RESET}"
    echo -e "  Следом на основном сервере: ${CYAN}bash scripts/production_vpn_gate.sh SUBSCRIPTION_ID${RESET}"
else
    echo -e "  Статус:    ${YELLOW}⚠️  Требуется ручная регистрация${RESET}"
    echo -e "  API URL:   ${YELLOW}$PUBLIC_API_URL${RESET}"
    echo -e "  Логин:     ${YELLOW}$MARZBAN_USER${RESET} / Пароль: ${YELLOW}$MARZBAN_PASS${RESET}"
fi
echo
echo -e "${BOLD}📊 Статус Marzban:${RESET}"
docker compose -f "$MARZBAN_DIR/docker-compose.yml" ps 2>/dev/null || true
echo
