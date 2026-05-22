#!/usr/bin/env bash
# Private Line VPN Bot — VPN Node Installer (Outline Manager)
# Run as root on a fresh Ubuntu 20.04/22.04/24.04 server.
set -euo pipefail

# ─── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET} $*"; }
success() { echo -e "${GREEN}[OK]${RESET}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET} $*"; }
error()   { echo -e "${RED}[ERR]${RESET}  $*" >&2; }
section() { echo -e "\n${BOLD}${BLUE}══ $* ══${RESET}"; }
die()     { error "$*"; exit 1; }

spinner() {
    local pid=$1 msg=$2
    local i=0 chars="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    while kill -0 "$pid" 2>/dev/null; do
        printf "\r${CYAN}%s${RESET} %s" "${chars:$i:1}" "$msg"
        i=$(( (i+1) % ${#chars} ))
        sleep 0.1
    done
    printf "\r"
}

prompt() {
    local var=$1 msg=$2 default=${3:-}
    if [[ -n "$default" ]]; then
        read -rp "$(echo -e "${YELLOW}▶ ${msg} [${default}]: ${RESET}")" val
        eval "$var='${val:-$default}'"
    else
        read -rp "$(echo -e "${YELLOW}▶ ${msg}: ${RESET}")" val
        eval "$var='$val'"
    fi
}

# ─── Pre-checks ───────────────────────────────────────────────────────────────
section "Проверка системы"

[[ $EUID -eq 0 ]] || die "Запустите установщик от root: sudo ./install_node.sh"

OS_ID=$(grep -oP '(?<=^ID=).+' /etc/os-release | tr -d '"' || true)
OS_VER=$(grep -oP '(?<=^VERSION_ID=).+' /etc/os-release | tr -d '"' || true)
case "$OS_ID" in
    ubuntu|debian) success "ОС: $OS_ID $OS_VER" ;;
    *) die "Поддерживаются только Ubuntu и Debian. Обнаружена: $OS_ID" ;;
esac

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
    curl -fsSL https://get.docker.com | bash >/dev/null 2>&1 &
    spinner $! "Установка Docker (1–2 минуты)..."
    systemctl enable docker --now >/dev/null 2>&1
    success "Docker установлен: $(docker --version)"
else
    success "Docker уже установлен: $(docker --version)"
fi

# ─── Collect server info ──────────────────────────────────────────────────────
section "Информация о сервере"

echo -e "${CYAN}Введите информацию о расположении сервера (для отображения в боте):${RESET}\n"

prompt SERVER_NAME "Название сервера (например: Germany-1)" "Server-1"
prompt COUNTRY     "Страна (на английском, например: Germany)" "Germany"
prompt CITY        "Город (например: Frankfurt)" ""
prompt COUNTRY_CODE "Код страны ISO (например: de, nl, us)" ""

# Suggest flag emoji based on country code
if [[ -n "$COUNTRY_CODE" ]]; then
    # Convert country code to flag emoji (regional indicator symbols)
    cc_upper=$(echo "$COUNTRY_CODE" | tr '[:lower:]' '[:upper:]')
    flag=""
    for (( i=0; i<${#cc_upper}; i++ )); do
        c="${cc_upper:$i:1}"
        code=$(printf '%d' "'$c")
        regional=$(( code - 65 + 127462 ))
        flag+=$(printf "\\U$(printf '%08X' $regional)")
    done
    prompt FLAG_EMOJI "Флаг (эмодзи)" "${flag}"
else
    prompt FLAG_EMOJI "Флаг (эмодзи, например: 🇩🇪)" "🌐"
fi

# ─── Run Outline Manager installer ───────────────────────────────────────────
section "Установка Outline Manager"

info "Скачиваю и запускаю официальный установщик Outline..."
echo -e "${YELLOW}Это займёт 2–5 минут. Не закрывайте терминал!${RESET}\n"

OUTLINE_OUTPUT_FILE=$(mktemp)

# Run Outline installer, capture its output
bash -c "$(wget -qO- https://raw.githubusercontent.com/Jigsaw-Code/outline-server/master/src/server_manager/install_scripts/install_server.sh)" \
    2>&1 | tee "$OUTLINE_OUTPUT_FILE"

echo

# ─── Parse Outline output ─────────────────────────────────────────────────────
section "Извлечение данных подключения"

# Extract the JSON config line from Outline output
OUTLINE_JSON=$(grep -oP '\{[^{}]*"apiUrl"[^{}]*"certSha256"[^{}]*\}' "$OUTLINE_OUTPUT_FILE" \
    || grep -oP '\{[^{}]*"certSha256"[^{}]*"apiUrl"[^{}]*\}' "$OUTLINE_OUTPUT_FILE" \
    || true)

if [[ -z "$OUTLINE_JSON" ]]; then
    # Try alternative: look for ShadowBox config
    OUTLINE_JSON=$(grep -oP 'ss-server-config:.*' "$OUTLINE_OUTPUT_FILE" | grep -oP '\{.*\}' || true)
fi

if [[ -z "$OUTLINE_JSON" ]]; then
    warn "Не удалось автоматически извлечь конфигурацию Outline."
    warn "Найдите в выводе выше строку вида:"
    warn '  {"apiUrl":"https://...","certSha256":"..."}'
    warn "и скопируйте её вручную."
    prompt OUTLINE_JSON "Вставьте JSON строку конфигурации"
fi

if [[ -n "$OUTLINE_JSON" ]]; then
    API_URL=$(echo "$OUTLINE_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('apiUrl',''))" 2>/dev/null \
        || echo "$OUTLINE_JSON" | grep -oP '"apiUrl"\s*:\s*"\K[^"]+' || true)
    CERT_SHA256=$(echo "$OUTLINE_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('certSha256',''))" 2>/dev/null \
        || echo "$OUTLINE_JSON" | grep -oP '"certSha256"\s*:\s*"\K[^"]+' || true)
else
    API_URL=""
    CERT_SHA256=""
fi

if [[ -z "$API_URL" ]]; then
    warn "Не удалось извлечь API URL автоматически."
    prompt API_URL "Введите API URL вручную (из вывода Outline)"
fi
if [[ -z "$CERT_SHA256" ]]; then
    warn "Не удалось извлечь Cert SHA256 автоматически."
    prompt CERT_SHA256 "Введите Cert SHA256 вручную (из вывода Outline)"
fi

# Extract management port from API URL
MGMT_PORT=$(echo "$API_URL" | grep -oP ':\K[0-9]+(?=/)' || echo "")

rm -f "$OUTLINE_OUTPUT_FILE"

# ─── Firewall setup ───────────────────────────────────────────────────────────
section "Настройка файерволла (UFW)"

ufw --force reset >/dev/null 2>&1
ufw default deny incoming >/dev/null 2>&1
ufw default allow outgoing >/dev/null 2>&1
ufw allow 22/tcp comment "SSH" >/dev/null 2>&1

if [[ -n "$MGMT_PORT" ]]; then
    ufw allow "${MGMT_PORT}/tcp" comment "Outline management" >/dev/null 2>&1
    success "Открыт порт управления: $MGMT_PORT/tcp"
fi

# Outline access port (typically random, shown in Docker ps)
OUTLINE_ACCESS_PORT=$(docker ps --format '{{.Ports}}' 2>/dev/null \
    | grep -oP '0\.0\.0\.0:\K[0-9]+(?=->)' | grep -v "$MGMT_PORT" | head -1 || true)
if [[ -n "$OUTLINE_ACCESS_PORT" ]]; then
    ufw allow "${OUTLINE_ACCESS_PORT}/tcp" comment "Outline access TCP" >/dev/null 2>&1
    ufw allow "${OUTLINE_ACCESS_PORT}/udp" comment "Outline access UDP" >/dev/null 2>&1
    success "Открыты порты доступа: $OUTLINE_ACCESS_PORT/tcp и /udp"
else
    ufw allow 443/tcp comment "Outline access (443)" >/dev/null 2>&1
    ufw allow 443/udp comment "Outline access (443)" >/dev/null 2>&1
    warn "Не определён порт доступа. Открыты 443/tcp и 443/udp."
fi

ufw --force enable >/dev/null 2>&1
success "UFW файерволл включён"

# ─── Save node info ───────────────────────────────────────────────────────────
section "Сохранение конфигурации"

INFO_FILE="/root/outline_node_info.txt"
cat > "$INFO_FILE" <<EOF
=== Private Line VPN Node ===
Установлено: $(date '+%Y-%m-%d %H:%M:%S')

Сервер:      $SERVER_NAME
Страна:      $COUNTRY
Город:       ${CITY:-—}
Код страны:  ${COUNTRY_CODE:-—}
Флаг:        ${FLAG_EMOJI:-🌐}
IP:          $SERVER_IP

=== Данные для добавления в Admin Panel ===
API URL:     $API_URL
Cert SHA256: $CERT_SHA256
EOF

success "Конфигурация сохранена: $INFO_FILE"

# ─── Optional SSH hardening ───────────────────────────────────────────────────
section "Дополнительная защита"

echo -e "${YELLOW}Хотите отключить вход по паролю SSH (рекомендуется, если есть SSH-ключ)?${RESET}"
read -rp "$(echo -e "${YELLOW}▶ Отключить SSH-пароль? [y/N]: ${RESET}")" DISABLE_SSH_PASS

if [[ "${DISABLE_SSH_PASS,,}" == "y" ]]; then
    sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
    systemctl restart sshd 2>/dev/null || systemctl restart ssh 2>/dev/null || true
    success "Вход по SSH-паролю отключён. Убедитесь, что ваш SSH-ключ добавлен!"
    warn "ВАЖНО: Не закрывайте текущую сессию, пока не проверите вход по ключу!"
fi

# Unattended upgrades
if command -v apt-get &>/dev/null; then
    apt-get install -y -qq unattended-upgrades >/dev/null 2>&1
    echo 'Unattended-Upgrade::Automatic-Reboot "false";' \
        > /etc/apt/apt.conf.d/51private-line
    success "Автоматические обновления безопасности включены"
fi

# ─── Final summary ────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║       Private Line VPN Node — Установка завершена!  ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════╝${RESET}"
echo
echo -e "${BOLD}📋 Информация о ноде:${RESET}"
echo -e "  Сервер:      ${CYAN}$SERVER_NAME${RESET}"
echo -e "  Страна:      ${CYAN}$COUNTRY${RESET}"
if [[ -n "${CITY:-}" ]]; then
echo -e "  Город:       ${CYAN}$CITY${RESET}"
fi
echo -e "  IP:          ${CYAN}$SERVER_IP${RESET}"
echo
echo -e "${BOLD}🔑 Данные для добавления в Admin Panel:${RESET}"
echo -e "  API URL:${RESET}"
echo -e "  ${YELLOW}$API_URL${RESET}"
echo
echo -e "  Cert SHA256:${RESET}"
echo -e "  ${YELLOW}$CERT_SHA256${RESET}"
echo
echo -e "${BOLD}⚡ Следующий шаг:${RESET}"
echo -e "  1. Войдите в Admin Panel основного сервера"
echo -e "  2. Перейдите: Серверы → Добавить сервер"
echo -e "  3. Введите ${BOLD}API URL${RESET} и ${BOLD}Cert SHA256${RESET} из строк выше"
echo -e "  4. Нажмите «Проверить соединение» — должна появиться зелёная галочка"
echo
echo -e "${BOLD}📁 Конфигурация сохранена:${RESET} $INFO_FILE"
echo
echo -e "${BOLD}📊 Статус Outline:${RESET}"
docker ps --filter "name=shadowbox" --format "  {{.Names}}: {{.Status}}" 2>/dev/null || true
echo
