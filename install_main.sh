#!/usr/bin/env bash
# Private Line VPN Bot — Main Server Installer
# Run as root on a fresh Ubuntu 20.04/22.04/24.04 or Debian 11/12 server.
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

prompt_secret() {
    local var=$1 msg=$2
    read -rsp "$(echo -e "${YELLOW}▶ ${msg}: ${RESET}")" val
    echo
    eval "$var='$val'"
}

# ─── Pre-checks ───────────────────────────────────────────────────────────────
section "Проверка системы"

[[ $EUID -eq 0 ]] || die "Запустите установщик от root: sudo ./install_main.sh"

OS_ID=$(grep -oP '(?<=^ID=).+' /etc/os-release | tr -d '"' || true)
OS_VER=$(grep -oP '(?<=^VERSION_ID=).+' /etc/os-release | tr -d '"' || true)
case "$OS_ID" in
    ubuntu|debian) success "ОС: $OS_ID $OS_VER" ;;
    *) die "Поддерживаются только Ubuntu и Debian. Обнаружена: $OS_ID" ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
[[ -f "docker-compose.yml" ]] || die "Запустите скрипт из корня проекта Private Line."
success "Директория проекта: $SCRIPT_DIR"

# ─── Install dependencies ─────────────────────────────────────────────────────
section "Установка зависимостей"

apt-get update -qq

install_pkg() {
    if ! command -v "$1" &>/dev/null; then
        info "Устанавливаю: $1..."
        apt-get install -y -qq "$2" >/dev/null 2>&1
        success "$1 установлен"
    else
        success "$1 уже установлен"
    fi
}

install_pkg "git"      "git"
install_pkg "openssl"  "openssl"
install_pkg "python3"  "python3"
install_pkg "curl"     "curl"
install_pkg "wget"     "wget"

if ! command -v docker &>/dev/null; then
    info "Устанавливаю Docker..."
    curl -fsSL https://get.docker.com | bash >/dev/null 2>&1 &
    spinner $! "Установка Docker (1–2 минуты)..."
    systemctl enable docker --now >/dev/null 2>&1
    success "Docker установлен: $(docker --version)"
else
    success "Docker уже установлен: $(docker --version)"
fi

if ! docker compose version &>/dev/null 2>&1; then
    info "Устанавливаю Docker Compose plugin..."
    apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1
    success "Docker Compose установлен"
else
    success "Docker Compose: $(docker compose version --short)"
fi

# ─── Collect configuration ───────────────────────────────────────────────────
section "Настройка конфигурации"

echo -e "${CYAN}Введите параметры вашего бота. Нажмите Enter для значения по умолчанию.${RESET}\n"

# BOT_TOKEN
while true; do
    prompt BOT_TOKEN "Токен Telegram-бота (@BotFather)"
    if [[ "$BOT_TOKEN" =~ ^[0-9]+:.{20,}$ ]]; then
        break
    fi
    warn "Токен должен выглядеть как: 1234567890:ABCDefgh..."
done

# ADMIN_CHAT_ID
while true; do
    prompt ADMIN_CHAT_ID "Ваш Telegram ID (узнайте у @userinfobot)"
    [[ "$ADMIN_CHAT_ID" =~ ^-?[0-9]+$ ]] && break
    warn "ID должен быть числом (например: 123456789)"
done

# Domain
prompt DOMAIN "Домен для SSL (например: vpn.example.com, Enter = использовать IP)" ""
USE_DOMAIN=false
[[ -n "$DOMAIN" ]] && USE_DOMAIN=true

# Database password
gen_password() { openssl rand -base64 18 | tr -dc 'a-zA-Z0-9' | head -c 20; }
POSTGRES_PASSWORD=$(gen_password)
prompt POSTGRES_PASSWORD "Пароль PostgreSQL (Enter = случайный)" "$POSTGRES_PASSWORD"

# Grafana
prompt GRAFANA_PASSWORD "Пароль Grafana (для мониторинга)" "admin"

# Admin panel credentials
prompt ADMIN_USERNAME "Логин для Admin Panel" "admin"
while true; do
    prompt_secret ADMIN_PASSWORD "Пароль для Admin Panel"
    [[ ${#ADMIN_PASSWORD} -ge 8 ]] && break
    warn "Пароль должен быть не менее 8 символов"
done

# Payment provider
echo -e "\n${CYAN}Платёжные системы:${RESET}"
echo "  manual    — ручные платежи (по умолчанию)"
echo "  yookassa  — ЮКасса"
echo "  lava      — LAVA"
echo "  stars     — Telegram Stars"
echo "  crypto    — CryptoBot"
prompt PAYMENT_PROVIDER "Платёжная система" "manual"

YOOKASSA_SHOP_ID="" YOOKASSA_SECRET_KEY=""
LAVA_PROJECT_ID="" LAVA_SECRET_KEY=""
CRYPTO_BOT_TOKEN=""

case "$PAYMENT_PROVIDER" in
    yookassa)
        prompt YOOKASSA_SHOP_ID "YooKassa Shop ID"
        prompt_secret YOOKASSA_SECRET_KEY "YooKassa Secret Key"
        ;;
    lava)
        prompt LAVA_PROJECT_ID "LAVA Project ID"
        prompt_secret LAVA_SECRET_KEY "LAVA Secret Key"
        ;;
    crypto)
        prompt_secret CRYPTO_BOT_TOKEN "CryptoBot Token"
        ;;
esac

# Generate encryption keys
ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null \
    || openssl rand -base64 32)
SECRET_KEY=$(openssl rand -hex 32)

WEBHOOK_BASE_URL=""
if [[ "$USE_DOMAIN" == "true" ]]; then
    WEBHOOK_BASE_URL="https://${DOMAIN}"
fi

# ─── Create directories ───────────────────────────────────────────────────────
section "Создание директорий"
mkdir -p data backups logs nginx/ssl
success "Директории созданы: data/ backups/ logs/ nginx/ssl/"

# ─── Write .env file ─────────────────────────────────────────────────────────
section "Создание .env файла"

cat > .env <<EOF
# ── Telegram ──────────────────────────────────────────────────────────
BOT_TOKEN=${BOT_TOKEN}
ADMIN_CHAT_ID=${ADMIN_CHAT_ID}
BOT_USERNAME=

# ── Database ──────────────────────────────────────────────────────────
DATABASE_URL=postgresql+asyncpg://privateline:${POSTGRES_PASSWORD}@db:5432/privateline
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}

# ── Security ──────────────────────────────────────────────────────────
ENCRYPTION_KEY=${ENCRYPTION_KEY}
SECRET_KEY=${SECRET_KEY}

# ── Admin panel ───────────────────────────────────────────────────────
ADMIN_USERNAME=${ADMIN_USERNAME}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
SESSION_TIMEOUT_MINUTES=60
MAX_LOGIN_ATTEMPTS=5
LOGIN_LOCKOUT_MINUTES=30

# ── Monitoring ────────────────────────────────────────────────────────
GRAFANA_PASSWORD=${GRAFANA_PASSWORD}

# ── Payment ───────────────────────────────────────────────────────────
PAYMENT_PROVIDER=${PAYMENT_PROVIDER}
WEBHOOK_BASE_URL=${WEBHOOK_BASE_URL}
PAYMENT_INSTRUCTIONS=Свяжитесь с поддержкой для оплаты.
STARS_PER_RUB=0.5

# YooKassa
YOOKASSA_SHOP_ID=${YOOKASSA_SHOP_ID}
YOOKASSA_SECRET_KEY=${YOOKASSA_SECRET_KEY}

# LAVA
LAVA_PROJECT_ID=${LAVA_PROJECT_ID}
LAVA_SECRET_KEY=${LAVA_SECRET_KEY}

# CryptoBot
CRYPTO_BOT_TOKEN=${CRYPTO_BOT_TOKEN}

# YooMoney / FreeKassa / Robokassa / Prodamus (заполните при необходимости)
YOOMONEY_WALLET=
YOOMONEY_NOTIFICATION_SECRET=
FREEKASSA_MERCHANT_ID=
FREEKASSA_SECRET_1=
FREEKASSA_SECRET_2=
ROBOKASSA_MERCHANT_LOGIN=
ROBOKASSA_PASSWORD_1=
ROBOKASSA_PASSWORD_2=
PRODAMUS_API_URL=
PRODAMUS_WEBHOOK_SECRET=

# ── Application ───────────────────────────────────────────────────────
APP_VERSION=1.0.0
BACKUP_DIR=./backups
MAINTENANCE_MODE=false
REFUND_WINDOW_DAYS=7
REFUND_MAX_TRAFFIC_MB=100.0
ADMIN_ALLOWED_IPS=
BACKUP_BOT_TOKEN=
TELEGRAM_API_URL=
EOF

chmod 600 .env
success ".env создан (права 600)"

# ─── SSL certificate ─────────────────────────────────────────────────────────
section "Настройка SSL-сертификата"

if [[ "$USE_DOMAIN" == "true" ]]; then
    info "Пытаюсь получить Let's Encrypt сертификат для $DOMAIN..."
    if ! command -v certbot &>/dev/null; then
        apt-get install -y -qq certbot >/dev/null 2>&1
    fi
    if certbot certonly --standalone --non-interactive --agree-tos \
        --register-unsafely-without-email -d "$DOMAIN" \
        --pre-hook "docker compose stop nginx 2>/dev/null || true" \
        --post-hook "docker compose start nginx 2>/dev/null || true" \
        >/dev/null 2>&1; then
        cp "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" nginx/ssl/cert.pem
        cp "/etc/letsencrypt/live/${DOMAIN}/privkey.pem"  nginx/ssl/key.pem
        success "Let's Encrypt сертификат получен для $DOMAIN"

        # Auto-renewal cron
        (crontab -l 2>/dev/null; echo "0 3 * * * certbot renew --quiet --post-hook 'cp /etc/letsencrypt/live/${DOMAIN}/fullchain.pem ${SCRIPT_DIR}/nginx/ssl/cert.pem && cp /etc/letsencrypt/live/${DOMAIN}/privkey.pem ${SCRIPT_DIR}/nginx/ssl/key.pem && docker compose -f ${SCRIPT_DIR}/docker-compose.yml restart nginx'") | crontab -
        success "Автообновление сертификата настроено (cron 3:00)"
    else
        warn "Не удалось получить Let's Encrypt. Генерирую самоподписанный..."
        openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
            -keyout nginx/ssl/key.pem -out nginx/ssl/cert.pem \
            -subj "/CN=${DOMAIN}" -quiet 2>/dev/null
        warn "Используется самоподписанный сертификат. Браузер покажет предупреждение."
    fi
else
    info "Домен не указан — генерирую самоподписанный сертификат..."
    SERVER_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || echo "localhost")
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout nginx/ssl/key.pem -out nginx/ssl/cert.pem \
        -subj "/CN=${SERVER_IP}" -quiet 2>/dev/null
    success "Самоподписанный сертификат создан (CN=${SERVER_IP})"
fi
chmod 600 nginx/ssl/key.pem

# ─── Database migrations ──────────────────────────────────────────────────────
section "Запуск миграций базы данных"

info "Поднимаю базу данных..."
docker compose up -d db >/dev/null 2>&1
sleep 5  # Wait for postgres to initialize

info "Применяю миграции Alembic..."
docker compose run --rm bot python -m alembic upgrade head >/dev/null 2>&1 &
spinner $! "Применение миграций..."
success "Миграции применены"

# ─── Start services ───────────────────────────────────────────────────────────
section "Запуск всех сервисов"

docker compose up -d &
spinner $! "Запуск контейнеров (Docker Compose)..."
success "Контейнеры запущены"

# ─── Health check ─────────────────────────────────────────────────────────────
section "Проверка работоспособности"

info "Ожидаю готовности Admin Panel (до 60 секунд)..."
READY=false
for i in $(seq 1 12); do
    if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
        READY=true
        break
    fi
    sleep 5
done

if [[ "$READY" == "true" ]]; then
    success "Admin Panel запущена и отвечает"
else
    warn "Admin Panel не ответила за 60 секунд. Проверьте логи:"
    warn "  docker compose logs admin --tail=30"
fi

# ─── Final summary ────────────────────────────────────────────────────────────
SERVER_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')

echo
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║        Private Line VPN Bot — Установка завершена!  ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════╝${RESET}"
echo
echo -e "${BOLD}📋 Ваши данные:${RESET}"
echo -e "  Бот в Telegram:   ${CYAN}Найдите по токену в BotFather${RESET}"
echo -e "  Admin Panel:      ${CYAN}https://${DOMAIN:-$SERVER_IP}/admin${RESET}"
echo -e "  Grafana:          ${CYAN}http://${SERVER_IP}:3000${RESET}"
echo
echo -e "${BOLD}🔑 Учётные данные Admin Panel:${RESET}"
echo -e "  Логин:    ${YELLOW}${ADMIN_USERNAME}${RESET}"
echo -e "  Пароль:   ${YELLOW}${ADMIN_PASSWORD}${RESET}"
echo
echo -e "${BOLD}🔑 Grafana:${RESET}"
echo -e "  Логин:    ${YELLOW}admin${RESET}"
echo -e "  Пароль:   ${YELLOW}${GRAFANA_PASSWORD}${RESET}"
echo
echo -e "${BOLD}🗄 База данных PostgreSQL:${RESET}"
echo -e "  Пароль:   ${YELLOW}${POSTGRES_PASSWORD}${RESET}"
echo
echo -e "${BOLD}📁 Файлы конфигурации:${RESET}"
echo -e "  ${SCRIPT_DIR}/.env  — основные настройки"
echo
echo -e "${BOLD}⚡ Следующие шаги:${RESET}"
echo -e "  1. Установите VPN-ноду: ${CYAN}./install_node.sh${RESET} (на отдельном сервере)"
echo -e "  2. Добавьте ноду: Admin Panel → Серверы → Добавить сервер"
echo -e "  3. Добавьте тарифы: Admin Panel → Тарифы → Добавить тариф"
echo -e "  4. Запустите бота и напишите ему в Telegram!"
echo
echo -e "${BOLD}📚 Полная документация:${RESET} INSTALL.md"
echo
echo -e "${BOLD}Полезные команды:${RESET}"
echo -e "  ${CYAN}docker compose ps${RESET}              — статус сервисов"
echo -e "  ${CYAN}docker compose logs -f bot${RESET}     — логи бота"
echo -e "  ${CYAN}docker compose logs -f admin${RESET}   — логи admin panel"
echo -e "  ${CYAN}docker compose restart bot${RESET}     — перезапустить бота"
echo
