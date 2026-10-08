# Data Model: referral

Только **новые таблицы**; существующие (`users`, `orders`, `vpn_profiles`, `trials`) не меняются (Alembic в проекте нет). Все деньги - целые рубли, время - naive UTC (`src/domain/clock.py`).

## referral_links

Одноразовые ссылки пригласившего. Токен хранится открытым текстом (нужно повторно показывать ссылку), в логи не попадает.

| Колонка | Тип | Примечание |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| inviter_id | BIGINT NOT NULL FK users.id, INDEX | пригласивший |
| token | VARCHAR NOT NULL **UNIQUE** | `secrets.token_urlsafe(16)` |
| created_at | DATETIME NOT NULL | |
| revoked_at | DATETIME NULL | отзыв владельцем |
| used_at | DATETIME NULL | момент «сжигания» |
| invitee_id | BIGINT NULL FK users.id | кто использовал |

Неиспользованная ссылка: `used_at IS NULL AND revoked_at IS NULL`. Срока действия нет.

## referrals

Привязка приглашённого. Запись создаётся в той же транзакции, что «сжигание» ссылки.

| Колонка | Тип | Примечание |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| link_id | INTEGER NOT NULL FK referral_links.id **UNIQUE** | одна ссылка - одна привязка |
| inviter_id | BIGINT NOT NULL FK users.id | |
| invitee_id | BIGINT NOT NULL FK users.id **UNIQUE** | один раз навсегда |
| status | VARCHAR NOT NULL default `bound` | `bound` -> `rewarded` \| `closed` |
| close_reason | VARCHAR NULL | `no_cash`, `monthly_cap`, `no_reward`, `not_eligible`, `inviter_not_paid` |
| bound_at | DATETIME NOT NULL | |
| bonus_order_id | INTEGER NULL FK orders.id | заявка (claim) заказа на бонус и награду |
| reward_rub | INTEGER NULL | начисленная награда |
| resolved_at | DATETIME NULL | момент перехода в `rewarded`/`closed` |
| rewarded_at | DATETIME NULL | момент начисления (для месячного лимита) |
| notified_at | DATETIME NULL | захват флага уведомления (как `notified_*_at` у триала) |
| notify_blocked_at | DATETIME NULL | пригласивший заблокировал бота |

`CHECK (inviter_id <> invitee_id)`. Индекс `(inviter_id, status, rewarded_at)` для месячного лимита.

Жизненный цикл: `bound` (привязан, ждёт первого заказа) -> `rewarded` (награда записана в журнал) или `closed` (без награды: деньгами 0, лимит месяца, награда округлилась до 0, заказ завершён без заявки).

## balance_entries

Append-only журнал. Записи не обновляются и не удаляются. Баланс = `SUM(amount_rub)` по пользователю.

| Колонка | Тип | Примечание |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| user_id | BIGINT NOT NULL FK users.id, INDEX | |
| kind | VARCHAR NOT NULL | `referral_reward` (+), `order_spend` (-), `order_refund` (+), `admin_adjust` (±, только по согласованию) |
| amount_rub | INTEGER NOT NULL, `CHECK (amount_rub <> 0)` | со знаком |
| ref_id | INTEGER NULL | `referral_reward` -> referrals.id; `order_spend`/`order_refund` -> orders.id |
| note | VARCHAR NULL | комментарий (для `admin_adjust`) |
| created_at | DATETIME NOT NULL | |

`UNIQUE (kind, ref_id)`: награда за привязку - один раз, удержание за заказ - один раз, возврат за заказ - один раз. Для `admin_adjust` `ref_id` NULL (в SQLite NULL в UNIQUE допускает повторы).

## order_payments

1:1 с заказом; финансовые параметры, зафиксированные при создании заказа. Заказы без строки (созданные до релиза) трактуются как `balance_rub = 0, cash_rub = цена по тарифу`; строка создаётся лениво (`INSERT OR IGNORE`) при расчёте плана.

| Колонка | Тип | Примечание |
|---|---|---|
| order_id | INTEGER PK FK orders.id | |
| price_rub | INTEGER NOT NULL | цена на момент создания |
| balance_rub | INTEGER NOT NULL default 0 | удержано с баланса |
| cash_rub | INTEGER NOT NULL | деньгами; база награды |
| bonus_bytes | BIGINT NOT NULL default 0 | бонус приглашённого, вошедший в план |
| created_at | DATETIME NOT NULL | |

`CHECK (balance_rub >= 0 AND cash_rub >= 0 AND balance_rub + cash_rub = price_rub)`.

## Право приглашать (без новых колонок)

Пользователь «может приглашать», если существует заказ `completed`, у которого нет строки в `order_payments` (создан до релиза фичи) либо `cash_rub > 0`:

```sql
SELECT 1 FROM orders o LEFT JOIN order_payments p ON p.order_id = o.id
WHERE o.user_id = :user AND o.status = 'completed' AND (p.order_id IS NULL OR p.cash_rub > 0) LIMIT 1;
```

Проверяется при показе/создании ссылки и в транзакции завершения заказа приглашённого (перед записью награды).

## Ключевые атомарные операторы

Правило: **первый оператор каждой транзакции - запись** (или условный `INSERT ... SELECT`), чтобы SQLite брал write-lock сразу и ждал по busy-timeout, а не падал на апгрейде read -> write; на всякий случай обёртка повторяет транзакцию при `database is locked` (до 3 раз).

```sql
-- лимит активных ссылок (атомарно)
INSERT INTO referral_links (inviter_id, token, created_at)
SELECT :inviter, :token, :now
WHERE (SELECT COUNT(*) FROM referral_links
       WHERE inviter_id = :inviter AND used_at IS NULL AND revoked_at IS NULL) < :max;

-- сжигание ссылки при привязке (rowcount = 1 -> привязано); платный заказ проверяется тут же
UPDATE referral_links SET used_at = :now, invitee_id = :invitee
WHERE token = :token AND used_at IS NULL AND revoked_at IS NULL AND inviter_id <> :invitee
  AND NOT EXISTS (SELECT 1 FROM orders WHERE user_id = :invitee AND status = 'completed');
-- затем INSERT INTO referrals (...); IntegrityError (invitee уже привязан) -> ROLLBACK всей транзакции, ссылка не сгорает

-- удержание баланса при создании заказа (rowcount = 0 -> баланс изменился, пересчитать)
INSERT INTO balance_entries (user_id, kind, amount_rub, ref_id, created_at)
SELECT :user, 'order_spend', -:hold, :order_id, :now
WHERE (SELECT COALESCE(SUM(amount_rub), 0) FROM balance_entries WHERE user_id = :user) >= :hold;

-- заявка на бонус (claim)
UPDATE referrals SET bonus_order_id = :order
WHERE invitee_id = :user AND status = 'bound' AND bonus_order_id IS NULL;

-- завершение заказа
UPDATE orders SET status = 'completed' WHERE id = :order AND status = 'pending';  -- rowcount 0 -> no-op

-- награда с проверкой месячного лимита
UPDATE referrals SET status = 'rewarded', reward_rub = :r, rewarded_at = :now, resolved_at = :now
WHERE id = :ref AND status = 'bound' AND bonus_order_id = :order
  AND (SELECT COUNT(*) FROM referrals x
       WHERE x.inviter_id = referrals.inviter_id AND x.status = 'rewarded'
         AND x.rewarded_at >= :month_start AND x.rewarded_at < :month_end) < :cap;
-- rowcount 1 -> INSERT INTO balance_entries (... 'referral_reward', +:r, ref_id = :ref);  иначе закрыть со 'monthly_cap'
```

## SQL для боевой БД (если применять вручную)

Файл `specs/004-referral/migration.sql`. Это только `CREATE TABLE IF NOT EXISTS`/`CREATE INDEX IF NOT EXISTS`: существующие таблицы не затрагиваются. Бот и сам создаёт недостающие таблицы при старте (`Base.metadata.create_all`), так что применять заранее нужно лишь для контроля; **до релиза обязательно сделать копию `data/bot.sqlite3`**.
