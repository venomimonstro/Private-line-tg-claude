# Private Line VPN Bot — Архитектура и техническая документация

## Стек

| Компонент | Технология |
|---|---|
| Telegram-бот | aiogram 3.x, Python |
| Admin-панель | FastAPI + Jinja2 |
| База данных | PostgreSQL + SQLAlchemy async |
| VPN-нод | Marzban (VLESS+Reality поверх XRay) |
| Деплой | Docker Compose (основной сервер) |
| Планировщик | APScheduler AsyncIOScheduler |

## Серверная топология

```
Основной сервер (155.212.224.153)          VPN-нод (78.17.33.214)
┌────────────────────────────────┐         ┌────────────────────────┐
│  bot        (aiogram)          │         │  Marzban (port 2096)   │
│  admin      (FastAPI :8000)    │◄───────►│  XRay/VLESS port 443   │
│  nginx      (:80, :29148)      │         │  db.sqlite3            │
│  postgres                      │         │  xray_config.json      │
│  scheduler  (APScheduler)      │         └────────────────────────┘
└────────────────────────────────┘
```

Nginx на основном сервере:
- `:80` — webhook Telegram + `/sub/`
- `:29148` — admin-панель + портал пользователя + `/sub/`

## Протокол VPN: VLESS + Reality

Reality — это маскировка: сервер показывает TLS-сертификат Microsoft (или другого сайта),
внутри которого идёт VPN-трафик. DPI его не видит.

Структура ключа:
```
vless://UUID@HOST:PORT?security=reality&pbk=PUBLIC_KEY&sid=SHORT_ID&sni=SNI&flow=xtls-rprx-vision
```

- `UUID` — уникален для каждого пользователя, хранится в Marzban db.sqlite3 и в нашей БД (`access_url_encrypted`)
- `pbk` — публичный ключ Reality, генерируется один раз, хранится в `xray_config.json` на ноде
- `sid`, `sni` — из той же конфигурации XRay

**Ключ не меняется** пока:
1. UUID пользователя в Marzban совпадает с тем что в ключе клиента
2. `pbk` (публичный ключ) не изменился

## Механизмы стабильности ключей

### 1. Guard-скрипт на ноде (`/usr/local/bin/marzban-db-guard.sh`, cron каждую минуту)

Запускается на VPN-ноде `78.17.33.214`. Делает три вещи:

**а) Проверка БД Marzban**
- Если `db.sqlite3` == 0 байт (вайп) → восстанавливает из `/var/lib/marzban/backups/` → `systemctl restart marzban`
- Резервная копия делается каждую минуту если БД здоровая
- Старые бэкапы (>3 дней) удаляются

**б) Проверка порта 443**
- TCP-проверка на порт 443 → если недоступен → `systemctl restart marzban`

**Результат**: при вайпе БД максимальная пауза ≤ 1 минута (cron + restart).
Все UUID восстанавливаются из бэкапа → клиентские ключи работают БЕЗ изменений.

### 2. `repair_marzban_keys()` — шедулер каждые 15 минут

Файл: `app/services/vpn_key.py` → функция `repair_marzban_keys()`
Задача: `scheduler/tasks.py` → `_job_repair_marzban_inbounds`

**Что делает:**
1. Получает список всех пользователей из Marzban через `get_transfer_stats()`
2. Для каждого активного ключа в нашей БД проверяет:
   - **URL сломан** (`_key_url_needs_repair`): пустой, нет `pbk`/`sid`, `sni` содержит `[` (markdown-артефакт)
   - **Пользователь отсутствует в Marzban** (DB wipe, пользователь создан после последнего бэкапа)
3. Если нужен ремонт → вызывает `client.create_key(name=..., uuid=preserved_uuid)`

**UUID preservation** (ключевая фича):
При пересоздании пользователя после вайпа БД repair-джоб извлекает UUID из нашего зашифрованного `access_url` и передаёт его в Marzban. Marzban создаёт пользователя с тем же UUID → итоговый ключ **идентичен** старому → клиент не замечает ничего.

### 3. Шифрование ключей в БД

Все `access_url` хранятся зашифрованными Fernet (`app/encryption.py`).
Ключ шифрования: `FERNET_KEY` из `.env`.

### 4. Circuit Breaker

`app/services/circuit_breaker.py` — защищает от каскадных сбоев при недоступности ноды.
Если нода не отвечает → CB открывается → операции ставятся в очередь (`outline_queue`).

### 5. Очередь отложенных операций

`app/services/outline_queue.py` — при сбое создания ключа операция сохраняется в БД и повторяется.

## Ключевые файлы

| Файл | Назначение |
|---|---|
| `app/vpn/marzban.py` | Клиент Marzban API. `create_key(uuid=...)` поддерживает сохранение UUID |
| `app/services/vpn_key.py` | Создание, ремонт, инвалидация ключей. `repair_marzban_keys()` |
| `scheduler/tasks.py` | APScheduler задачи. Ремонт каждые 15 минут |
| `app/services/user_notifications.py` | Уведомления пользователям (ключ готов, оплата, сбой) |
| `bot/handlers/access.py` | Бот: «Мой доступ», «Получить ключ», «Обновить ключ» |
| `bot/keyboards/main_menu.py` | Главная клавиатура бота |
| `app/services/subscription.py` | Активация подписки после оплаты |
| `nginx/nginx.conf` | Проксирование на :80 и :29148 |

## Таблицы БД (ключевые)

**`vpn_keys`**
- `outline_key_id` — Marzban username (`u{user_id}s{sub_id}`, max 32 chars)
- `access_url_encrypted` — зашифрованный vless:// URL
- `traffic_used_gb` — накопленный трафик (не сбрасывается при перевыпуске)
- `reissue_count_today` / `reissue_date` — счётчик ручных перевыпусков (лимит 5/день)

## Деплой

```bash
# На основном сервере
cd /opt/private-line
git pull origin claude/private-line-mvp-cmx4Z
docker compose restart bot admin
# nginx перезапускать только если менялся nginx.conf:
docker compose restart nginx
```

Ветка разработки: `claude/private-line-mvp-cmx4Z`

## История значимых изменений

### 2026-06 — Стабильность ключей

**Проблема**: Marzban `db.sqlite3` иногда обнулялся → все пользователи исчезали → ключи переставали работать.

**Решения**:
1. Guard-скрипт на ноде (cron 1 мин) — автовосстановление БД из бэкапа, рестарт если порт 443 мёртв
2. `repair_marzban_keys()` — каждые 15 мин сверяет наличие пользователей в Marzban, пересоздаёт пропавших
3. **UUID preservation** — при пересоздании после вайпа передаём оригинальный UUID → ключ клиента не меняется
4. Трафик сохраняется при перевыпуске ключа (`traffic_used_gb` наследуется)
5. Уведомление пользователя при сбое активации (`notify_activation_failed`)

### 2026-06 — Интерфейс

- Убрана «ссылка-подписка» из сообщений бота (приложения её не поддерживают корректно)
- Компактная клавиатура: 5 строк вместо 7, «Поддержка» внизу
