# Data Model: free-trial

Существующие таблицы (`users`, `orders`, `vpn_profiles`) **не меняются**. Добавляется одна новая таблица `trials`.
`Base.metadata.create_all` (вызывается в `main.init_db()` при старте) создаёт отсутствующие таблицы, поэтому для `trials` отдельная миграция на боевой БД не нужна (см. ручной DDL ниже - только если хотите создать таблицу заранее/проверить).

## Trial (`trials`)

Один триал на один Telegram-аккаунт навсегда. Запись не удаляется после выдачи (удаляется только откат резерва при сбое выдачи).

| Поле | Тип | Описание |
|------|-----|----------|
| `id` | Integer, PK, autoincrement | |
| `telegram_id` | BigInteger, **UNIQUE**, NOT NULL, FK `users.id` | Уникальность на уровне БД - защита от дублей и гонок |
| `marzban_username` | String, UNIQUE, NOT NULL | `user_{telegram_id}_trial` |
| `status` | String, NOT NULL | `reserved` -> `active` -> `ended` -> `converted` (см. переходы) |
| `data_limit_bytes` | BigInteger, NOT NULL | Лимит на момент выдачи (смена конфига не ломает уже выданные триалы) |
| `created_at` | DateTime, NOT NULL, default now | Момент резерва; по нему считается суточный лимит |
| `granted_at` | DateTime, NULL | Выдан (пользователь создан в Marzban, профиль сохранён) |
| `expires_at` | DateTime, NULL | Окончание по сроку (`granted_at + TRIAL_DAYS`) |
| `reached_80_at` | DateTime, NULL | Достигнуто 80% трафика |
| `ended_at` | DateTime, NULL | Момент обнаружения окончания (трафик/срок) |
| `ended_reason` | String, NULL | `traffic` / `time` |
| `converted_at` | DateTime, NULL | Куплен платный пакет (подтверждение заказа в `approve_order`) |
| `notified_low_at` | DateTime, NULL | Флаг уведомления (a) «осталось мало» |
| `notified_ended_at` | DateTime, NULL | Флаг уведомления (b) «закончился» |
| `notified_reminder_at` | DateTime, NULL | Флаг уведомления (c) напоминание |
| `bot_blocked_at` | DateTime, NULL | Пользователь заблокировал бота - больше не писать |

Время хранится как naive UTC (как `CURRENT_TIMESTAMP` в SQLite); в Marzban `expire` передаётся unix-секундами.

Индексы: уникальный по `telegram_id`, уникальный по `marzban_username`, обычные по `status` и `created_at`.

### Переходы статуса

```text
(нет записи) --reserve--> reserved --activate--> active --обнаружено окончание--> ended --покупка--> converted
                              |                     |                                 
                              |                     +--------------покупка---------------> converted
                              +--сбой Marzban/БД: откат (DELETE)--> (нет записи)
```

- `reserved` - резерв сделан, пользователь в Marzban ещё не подтверждён. Если процесс упал в этом состоянии, повторное нажатие кнопки подхватывает резерв и ищет пользователя в Marzban по имени (переиспользует/создаёт).
- `converted` ставится при подтверждённой покупке из любого не-`converted` статуса; операция идемпотентна.
- Уведомления отправляются только при `status` in (`active`, `ended`), `bot_blocked_at IS NULL` и отсутствии подтверждённых заказов у пользователя.

### Условия отправки уведомлений (атомарный захват флага)

| Уведомление | Условие `UPDATE ... SET notified_X_at = now WHERE ...` |
|-------------|--------------------------------------------------------|
| (a) low | `status='active' AND reached_80_at IS NOT NULL AND notified_low_at IS NULL AND bot_blocked_at IS NULL` |
| (b) ended | `status='ended' AND notified_ended_at IS NULL AND bot_blocked_at IS NULL` |
| (c) reminder | `status='ended' AND ended_at <= now-24h AND notified_reminder_at IS NULL AND bot_blocked_at IS NULL` |

Отправка выполняется только если `UPDATE` затронул 1 строку (захват флага). При `Forbidden` - ставится `bot_blocked_at`; при временной ошибке флаг снимается (повтор на следующей проверке).

## Ручной DDL (необязателен, в точности то, что создаёт `create_all`)

```sql
CREATE TABLE trials (
    id INTEGER NOT NULL,
    telegram_id BIGINT NOT NULL,
    marzban_username VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    data_limit_bytes BIGINT NOT NULL,
    created_at DATETIME NOT NULL,
    granted_at DATETIME,
    expires_at DATETIME,
    reached_80_at DATETIME,
    ended_at DATETIME,
    ended_reason VARCHAR,
    converted_at DATETIME,
    notified_low_at DATETIME,
    notified_ended_at DATETIME,
    notified_reminder_at DATETIME,
    bot_blocked_at DATETIME,
    PRIMARY KEY (id),
    UNIQUE (telegram_id),
    FOREIGN KEY(telegram_id) REFERENCES users (id),
    UNIQUE (marzban_username)
);
CREATE INDEX ix_trials_status ON trials (status);
CREATE INDEX ix_trials_created_at ON trials (created_at);
```

Проверено: `create_all` на БД без таблицы `trials` создаёт только её (определения `users`, `orders`, `vpn_profiles` не меняются).

## Чтение существующих таблиц (без изменения схемы)

- `orders`: `EXISTS(status='completed')` - есть платный заказ; `EXISTS(status='pending')` - заказ в ожидании.
- `vpn_profiles`: создаётся обычная запись для триал-пользователя (`marzban_username`, `sub_url`, `status='active'`), благодаря чему существующий `CreateOrderUseCase` ставит покупке `order_type="topup"`, и `approve_order` идёт по ветке обновления.
- `users`: при резерве гарантируется строка пользователя (`get_or_create`) - `/start` сам её не создаёт.

## DTO / dataclass (domain, без зависимостей)

- `TrialConfig(enabled, data_gb, days, daily_cap)` - вход для use case, собирается из `Settings` в `main.py`.
- `TrialSnapshot(used_ratio, reached_low, ended, ended_reason)` - результат чистой функции `evaluate_trial`.
- `TrialActivationResult(kind, sub_url, data_gb, expires_at)`; `kind` in `GRANTED / ALREADY_ACTIVE / ALREADY_USED / NOT_ELIGIBLE / CAP_REACHED / DISABLED / ERROR`.
- `RenewalPlan` (существующий) переиспользуется для конверсии триала через новую функцию `build_trial_conversion_plan` (carried_over_bytes=0).
