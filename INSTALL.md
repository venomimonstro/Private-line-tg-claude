# Private Line VPN Bot — Руководство по установке

## Содержание

1. [Обзор архитектуры](#1-обзор-архитектуры)
2. [Требования](#2-требования)
3. [Быстрый старт (10 минут)](#3-быстрый-старт-10-минут)
4. [Установка основного сервера](#4-установка-основного-сервера)
5. [Установка VPN-ноды](#5-установка-vpn-ноды)
6. [Добавление ноды в систему](#6-добавление-ноды-в-систему)
7. [Настройка платёжных систем](#7-настройка-платёжных-систем)
8. [Мониторинг](#8-мониторинг)
9. [Резервное копирование и восстановление](#9-резервное-копирование-и-восстановление)
10. [Переменные окружения](#10-переменные-окружения)
11. [Обновление системы](#11-обновление-системы)
12. [Устранение проблем](#12-устранение-проблем)

---

## 1. Обзор архитектуры

```
 ┌─────────────────────────────────────────────────┐
 │              Основной сервер                     │
 │                                                 │
 │  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
 │  │  Telegram │  │  Admin   │  │  PostgreSQL  │  │
 │  │    Bot   │  │  Panel   │  │   Database   │  │
 │  └──────────┘  └──────────┘  └──────────────┘  │
 │  ┌──────────────────────────────────────────┐   │
 │  │  Prometheus + Grafana + Alertmanager     │   │
 │  └──────────────────────────────────────────┘   │
 │  ┌──────────┐                                   │
 │  │  Nginx   │  (reverse proxy, SSL)             │
 │  └──────────┘                                   │
 └─────────────────────────────────────────────────┘
              │ Outline API (HTTPS)
 ┌────────────▼────────┐  ┌─────────────────────┐
 │   VPN-нода 1        │  │   VPN-нода 2        │
 │   (Outline Manager) │  │   (Outline Manager) │
 │   Германия          │  │   Нидерланды        │
 └─────────────────────┘  └─────────────────────┘
```

**Основной сервер** — управляет ботом, базой данных, платежами, мониторингом.  
**VPN-ноды** — обеспечивают подключение пользователей через Shadowsocks (Outline).

---

## 2. Требования

### Основной сервер
| Параметр | Минимум | Рекомендуется |
|----------|---------|---------------|
| ОС | Ubuntu 20.04 | Ubuntu 22.04 LTS |
| CPU | 1 vCPU | 2 vCPU |
| RAM | 1 GB | 2 GB |
| Диск | 20 GB | 40 GB SSD |
| Сеть | 100 Mbps | 1 Gbps |

### VPN-нода (на каждую)
| Параметр | Минимум | Рекомендуется |
|----------|---------|---------------|
| ОС | Ubuntu 20.04 | Ubuntu 22.04 LTS |
| CPU | 1 vCPU | 2 vCPU |
| RAM | 512 MB | 1 GB |
| Диск | 10 GB | 20 GB SSD |
| Сеть | 1 Gbps | 1 Gbps |

### Что нужно подготовить заранее
- Telegram-бот (создайте через [@BotFather](https://t.me/BotFather))
- ID вашего Telegram-аккаунта (узнайте через [@userinfobot](https://t.me/userinfobot))
- Домен для основного сервера (опционально, но рекомендуется)

---

## 3. Быстрый старт (10 минут)

### Шаг 1 — Основной сервер

```bash
# Войдите на сервер как root:
ssh root@YOUR_MAIN_SERVER_IP

# Скачайте и запустите установщик:
apt-get install -y git
git clone https://github.com/YOUR_REPO/private-line-tg-claude /opt/private-line
cd /opt/private-line
chmod +x install_main.sh
./install_main.sh
```

Следуйте подсказкам. Установка занимает 5–10 минут.

### Шаг 2 — VPN-нода

```bash
# Войдите на сервер ноды как root:
ssh root@YOUR_NODE_IP

# Скачайте и запустите установщик ноды:
apt-get install -y git
git clone https://github.com/YOUR_REPO/private-line-tg-claude /tmp/private-line
chmod +x /tmp/private-line/install_node.sh
/tmp/private-line/install_node.sh
```

### Шаг 3 — Добавьте ноду в систему

После установки ноды у вас будет:
- **API URL**: `https://1.2.3.4:12345/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
- **Cert SHA256**: `ABCD1234...`

Войдите в админ-панель по адресу `https://YOUR_DOMAIN/admin`, перейдите в **Серверы** → **Добавить сервер** и введите эти данные.

---

## 4. Установка основного сервера

### Ручная установка (без установщика)

```bash
# 1. Установите Docker
curl -fsSL https://get.docker.com | bash
systemctl enable docker && systemctl start docker

# 2. Клонируйте репозиторий
git clone https://github.com/YOUR_REPO/private-line-tg-claude /opt/private-line
cd /opt/private-line

# 3. Создайте .env файл
cp .env.example .env
# Отредактируйте .env своими значениями:
nano .env

# 4. Создайте директории
mkdir -p data backups logs nginx/ssl

# 5. Сгенерируйте SSL-сертификат
# Вариант A — с доменом (Let's Encrypt):
apt-get install -y certbot
certbot certonly --standalone -d YOUR_DOMAIN
cp /etc/letsencrypt/live/YOUR_DOMAIN/fullchain.pem nginx/ssl/cert.pem
cp /etc/letsencrypt/live/YOUR_DOMAIN/privkey.pem nginx/ssl/key.pem

# Вариант B — без домена (самоподписанный):
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout nginx/ssl/key.pem -out nginx/ssl/cert.pem \
  -subj "/C=RU/ST=Moscow/L=Moscow/O=PrivateLine/CN=localhost"

# 6. Запустите миграции базы данных
docker compose run --rm bot python -m alembic upgrade head

# 7. Запустите все сервисы
docker compose up -d

# 8. Проверьте статус
docker compose ps
```

### Структура .env файла

Минимально необходимые переменные (полный список в [разделе 10](#10-переменные-окружения)):

```env
# Обязательные
BOT_TOKEN=1234567890:ABCDefghijklmnopqrstuvwxyz
ADMIN_CHAT_ID=123456789
DATABASE_URL=postgresql+asyncpg://privateline:YOURPASSWORD@db:5432/privateline
ENCRYPTION_KEY=base64-encoded-32-byte-key
SECRET_KEY=random-secret-string

# Пароли PostgreSQL и Grafana (совпадают с docker-compose.yml)
POSTGRES_PASSWORD=YOURPASSWORD
GRAFANA_PASSWORD=admin

# Платёжная система (manual = ручные платежи)
PAYMENT_PROVIDER=manual
```

### Генерация ключей шифрования

```bash
# ENCRYPTION_KEY (Fernet):
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# SECRET_KEY:
openssl rand -hex 32
```

---

## 5. Установка VPN-ноды

### Автоматическая установка

```bash
chmod +x install_node.sh
./install_node.sh
```

### Ручная установка

```bash
# 1. Установите Docker
curl -fsSL https://get.docker.com | bash

# 2. Запустите установщик Outline Manager
wget -qO- https://raw.githubusercontent.com/Jigsaw-Code/outline-server/master/src/server_manager/install_scripts/install_server.sh | bash

# Сохраните вывод! Он содержит API URL и fingerprint:
# {"apiUrl":"https://1.2.3.4:12345/xxxx","certSha256":"ABCD..."}

# 3. Откройте порты в файерволе
ufw allow 22/tcp
ufw allow <PORT_FROM_API_URL>/tcp  # порт из apiUrl
ufw allow 443/tcp                   # доступ пользователей
ufw --force enable
```

---

## 6. Добавление ноды в систему

1. Войдите в **Admin Panel** → `https://YOUR_DOMAIN/admin`
2. Перейдите в **Серверы** → **Добавить сервер**
3. Заполните поля:
   - **Название**: Германия-1
   - **Страна**: Germany
   - **Город**: Frankfurt
   - **Код страны**: de
   - **Эмодзи флага**: 🇩🇪
   - **URL API**: `https://1.2.3.4:12345/xxxx` (из вывода install_node.sh)
   - **Cert SHA256**: `ABCDEF...` (из вывода install_node.sh)
   - **Доступен для выбора**: ✓
4. Нажмите **Сохранить**
5. Нажмите **Проверить соединение** — должна появиться зелёная галочка

---

## 7. Настройка платёжных систем

### YooKassa (рекомендуется для РФ)

1. Зарегистрируйтесь на [yookassa.ru](https://yookassa.ru)
2. Получите Shop ID и Secret Key
3. В `.env`:
   ```env
   PAYMENT_PROVIDER=yookassa
   YOOKASSA_SHOP_ID=12345
   YOOKASSA_SECRET_KEY=your_secret_key
   WEBHOOK_BASE_URL=https://YOUR_DOMAIN
   ```
4. В кабинете YooKassa укажите Webhook URL: `https://YOUR_DOMAIN/webhook/yookassa`

### LAVA

```env
PAYMENT_PROVIDER=lava
LAVA_PROJECT_ID=your_project_id
LAVA_SECRET_KEY=your_secret_key
WEBHOOK_BASE_URL=https://YOUR_DOMAIN
```
Webhook URL для LAVA: `https://YOUR_DOMAIN/webhook/lava`

### Telegram Stars (встроено в Telegram)

```env
PAYMENT_PROVIDER=stars
STARS_PER_RUB=0.5
```
Не требует регистрации — работает через Telegram Payments API.

### Ручные платежи (по умолчанию)

```env
PAYMENT_PROVIDER=manual
PAYMENT_INSTRUCTIONS=Переведите на карту Сбербанк 4276 XXXX XXXX XXXX и пришлите чек.
```

---

## 8. Мониторинг

### Grafana Dashboard

Доступен по адресу: `http://YOUR_SERVER_IP:3000`  
Логин по умолчанию: `admin` / `admin` (или значение `GRAFANA_PASSWORD`)

Дашборд автоматически показывает:
- Активные подписки
- Платежи (успешные / неуспешные)
- Активации VPN-ключей
- Задержку обработки вебхуков
- Состояние circuit breakers

### Alertmanager

Настроен на отправку уведомлений в Telegram через бота.  
Конфигурация: `monitoring/alertmanager/config.yml`

Алерты срабатывают при:
- Недоступности бота > 5 минут
- Ошибках платежей > 10% за 5 минут
- Падении активных подписок > 20% за час
- Заполнении диска > 85%
- Провале бэкапа
- Высокой задержке вебхуков > 5 секунд

### Проверка здоровья системы

```bash
# Статус всех сервисов
docker compose ps

# Логи бота
docker compose logs -f bot --tail=100

# Логи админ-панели
docker compose logs -f admin --tail=100

# Ручная проверка здоровья
curl http://localhost:8000/health
```

---

## 9. Резервное копирование и восстановление

### Автоматические бэкапы

Ежедневный бэкап запускается автоматически в 04:00 UTC.  
Бэкапы хранятся в директории `backups/`.

### Ручной бэкап

```bash
docker compose exec bot python -c "
import asyncio
from app.database import async_session_maker
from app.enums import BackupType
from backup_manager.backup import create_backup

async def main():
    async with async_session_maker() as session:
        await create_backup(BackupType.manual, session)

asyncio.run(main())
"
```

### Восстановление из бэкапа

```bash
# Список бэкапов
ls -la backups/

# Восстановление PostgreSQL из последнего бэкапа
./scripts/restore.sh backups/daily_backup_2026-05-21.tar.gz

# Проверка целостности бэкапа
./scripts/verify_backup.sh
```

---

## 10. Переменные окружения

### Обязательные

| Переменная | Описание | Пример |
|-----------|----------|--------|
| `BOT_TOKEN` | Токен Telegram-бота от @BotFather | `1234567890:ABC...` |
| `ADMIN_CHAT_ID` | ID Telegram-чата для уведомлений | `123456789` |
| `DATABASE_URL` | URL подключения к PostgreSQL | `postgresql+asyncpg://user:pass@db:5432/dbname` |
| `ENCRYPTION_KEY` | Fernet-ключ для шифрования VPN-ключей | `base64-encoded-key` |
| `SECRET_KEY` | Секретный ключ приложения | `random-hex-string` |

### База данных

| Переменная | По умолчанию | Описание |
|-----------|-------------|----------|
| `POSTGRES_PASSWORD` | `changeme` | Пароль PostgreSQL (в docker-compose.yml) |

### Платёжные системы

| Переменная | Описание |
|-----------|----------|
| `PAYMENT_PROVIDER` | Провайдер: `manual`, `yookassa`, `lava`, `stars`, `crypto`, `yoomoney`, `freekassa`, `robokassa`, `prodamus` |
| `WEBHOOK_BASE_URL` | Базовый URL для вебхуков (ваш домен) |
| `YOOKASSA_SHOP_ID` | Shop ID YooKassa |
| `YOOKASSA_SECRET_KEY` | Secret Key YooKassa |
| `LAVA_PROJECT_ID` | Project ID LAVA |
| `LAVA_SECRET_KEY` | Secret Key LAVA |
| `CRYPTO_BOT_TOKEN` | Токен CryptoBot |
| `STARS_PER_RUB` | Курс Telegram Stars к рублю (по умолчанию 0.5) |

### Мониторинг

| Переменная | По умолчанию | Описание |
|-----------|-------------|----------|
| `GRAFANA_PASSWORD` | `admin` | Пароль Grafana |

### Бот

| Переменная | По умолчанию | Описание |
|-----------|-------------|----------|
| `BOT_USERNAME` | — | Username бота (без @) |
| `BACKUP_BOT_TOKEN` | — | Токен резервного бота (при блокировке основного) |
| `TELEGRAM_API_URL` | — | Кастомный URL Telegram API (для анти-блокировки) |
| `ADMIN_ALLOWED_IPS` | — | Список IP для доступа в админку (через запятую) |

### Рефанды

| Переменная | По умолчанию | Описание |
|-----------|-------------|----------|
| `REFUND_WINDOW_DAYS` | `7` | Окно для рефанда (дней) |
| `REFUND_MAX_TRAFFIC_MB` | `100.0` | Макс. трафик для авторефанда (МБ) |

---

## 11. Обновление системы

```bash
cd /opt/private-line

# Остановите сервисы
docker compose down

# Получите обновления
git pull origin main

# Пересоберите образы
docker compose build

# Применит новые миграции
docker compose run --rm bot python -m alembic upgrade head

# Запустите обновлённые сервисы
docker compose up -d

# Проверьте логи
docker compose logs -f --tail=50
```

---

## 12. Устранение проблем

### Бот не отвечает

```bash
# Проверьте статус контейнера
docker compose ps bot

# Посмотрите логи
docker compose logs bot --tail=50

# Перезапустите
docker compose restart bot
```

### Ошибки подключения к базе данных

```bash
# Проверьте PostgreSQL
docker compose logs db --tail=20

# Проверьте миграции
docker compose run --rm bot python -m alembic current
docker compose run --rm bot python -m alembic upgrade head
```

### Ошибки Outline API при выдаче ключей

```bash
# Проверьте доступность ноды из основного сервера
curl -k "https://NODE_IP:NODE_PORT/MANAGEMENT_PREFIX/server"

# Проверьте pendingoperations в БД
docker compose exec db psql -U privateline -c \
  "SELECT * FROM pending_operations WHERE completed_at IS NULL ORDER BY created_at DESC LIMIT 10;"
```

### Nginx не запускается (проблема с SSL)

```bash
# Проверьте наличие сертификатов
ls -la nginx/ssl/

# Сгенерируйте самоподписанный на время
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout nginx/ssl/key.pem -out nginx/ssl/cert.pem \
  -subj "/CN=localhost"

docker compose restart nginx
```

### Платежи не подтверждаются

1. Проверьте настройку вебхука в кабинете провайдера
2. Убедитесь, что `WEBHOOK_BASE_URL` доступен из интернета
3. Проверьте логи: `docker compose logs admin | grep webhook`
4. В Admin Panel → Платежи → найдите платёж → нажмите **Перепроверить статус**

### Полный сброс (осторожно: удаляет все данные)

```bash
docker compose down -v
rm -rf data/ logs/
docker compose up -d
docker compose run --rm bot python -m alembic upgrade head
```

---

## Поддержка

Если что-то пошло не так:
1. Соберите логи: `docker compose logs > /tmp/all_logs.txt`
2. Проверьте статус: `docker compose ps`
3. Опишите проблему с приложенными логами
