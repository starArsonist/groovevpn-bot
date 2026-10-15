# Implementation Plan: free-trial

**Branch**: `feature/trial` (от `dev`) | **Date**: 2026-10-06 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/002-free-trial/spec.md`

## Summary

Бесплатный пробный период (10 ГБ / 7 дней) по кнопке на стартовом экране, один на Telegram-аккаунт навсегда. Выдача через резерв записи с уникальностью в БД и создание пользователя в Marzban (с откатом резерва при сбое и переиспользованием существующего пользователя при повторе). Фоновая пакетная проверка шлёт максимум три уведомления с кнопками тарифов (флаги в БД). Покупка триал-пользователя идёт по существующей ветке обновления `approve_order`; остаток триала не переносится (решение на уровне вызывающего кода, чистая функция переноса не меняется), триал переходит в `converted`.

## Technical Context

**Language/Version**: Python 3.12+  
**Primary Dependencies**: `python-telegram-bot` 22.8, `SQLAlchemy` (async), `aiosqlite`, `httpx`, `loguru` - **новых зависимостей нет** (рекомендуемый вариант; альтернатива JobQueue требует extra `job-queue`, см. research.md)  
**Storage**: SQLite; одна **новая** таблица `trials`, существующие таблицы не меняются  
**Testing**: `pytest`, `pytest-asyncio` (`asyncio_mode = "auto"`); новые тесты на файловой временной SQLite (честные параллельные сессии), фейковый Marzban/Telegram; 13 существующих тестов не меняются  
**Target Platform**: Docker (docker-compose), один процесс бота  
**Project Type**: Telegram Bot (Service)  
**Performance Goals**: <= 7 пакетных запросов к Marzban за цикл проверки; отправка <= ~20 сообщений/с  
**Constraints**: Clean Architecture + DI; всё асинхронно; строгая типизация; логирование `loguru`; линтеры не добавляем  
**Scale/Scope**: <= 50 новых триалов/сутки, <= ~350 одновременно активных

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Clean Architecture**: ✅ Правила триала (оценка, план конверсии) - чистые функции в `domain`; сценарии - в `use_cases`; Telegram/БД/Marzban - адаптеры. Use case работает через порты (репозиторий, нотификатор, Marzban-клиент), переданные снаружи.
- **II. DI**: ✅ `TrialRepository`, `MarzbanClient`, `TelegramTrialNotifier`, `TrialConfig` создаются в `main.py` и передаются в use cases; внутри use cases/хендлеров клиенты не создаются. `AdminUseCases` получает `trial_repo` через конструктор.
- **III. Async I/O First**: ✅ Вся работа с БД/Marzban/Telegram через `async/await`; фон - `asyncio`-задача.
- **IV. Строгая типизация**: ✅ type hints во всём новом коде.
- **V. Подробное логирование**: ✅ `loguru` с telegram_id/статусом для выдачи, откатов, переходов, отправок, ошибок.
- Отклонение (не нарушение): `TrialRepository` принимает `async_sessionmaker`, а не сессию, из-за конкурентного фонового задания (см. research.md, Decision 5). Нарушений нет, `Complexity Tracking` пуст.

## Точки изменения существующего кода

| Файл | Изменение |
|------|-----------|
| `src/config.py` | + `trial_enabled=True`, `trial_data_gb=10`, `trial_days=7`, `trial_daily_cap=50` (с дефолтами, чтобы текущий `.env` не сломался) |
| `.env.example` | + 4 переменные `TRIAL_*` |
| `src/domain/models/__init__.py` | экспорт `Trial` (чтобы `create_all` создал таблицу) |
| `src/adapters/marzban/client.py` | + `get_users(usernames)` (пакетный `GET /api/users`); существующие методы без изменений |
| `src/use_cases/admin_use_cases.py` | + необязательный `trial_repo`; в `_apply_topup` ветка плана для триала; `mark_converted` перед `completed` в обеих ветках; сборка inbounds через общую функцию; строка «Перенесено» в сообщении скрывается при 0 |
| `src/adapters/tg_bot/handlers/start.py` | кнопка «Попробовать бесплатно» первой, если пользователю предлагается триал |
| `src/adapters/tg_bot/handlers/payment.py` | (по желанию) отметка «после пробного периода» в уведомлении админу |
| `src/main.py` | DI, регистрация `CallbackQueryHandler(pattern="^trial_start$")`, запуск/остановка фоновой задачи |

Чистая функция `calculate_renewal_plan` и существующие тесты **не меняются**.

## Новые файлы

```text
src/
├── domain/
│   ├── clock.py                        # utc_now (naive UTC), to_unix / from_unix
│   ├── models/trial.py                 # SQLAlchemy-модель Trial
│   └── trial_rules.py                  # чистые функции: evaluate_trial, build_trial_conversion_plan, TrialConfig
├── use_cases/
│   ├── trial_use_cases.py              # ActivateTrialUseCase, TrialOfferUseCase (+ результаты)
│   └── trial_monitor.py                # TrialMonitorUseCase.run_once, порт TrialNotifier
├── adapters/
│   ├── db/trial_repository.py          # TrialRepository (session factory)
│   ├── marzban/inbounds.py             # build_inbounds_payload (вынесено из approve_order)
│   ├── scheduler.py                    # run_periodically
│   └── tg_bot/
│       ├── handlers/trial.py           # обработчик кнопки «Попробовать бесплатно»
│       └── notifier.py                 # TelegramTrialNotifier (тексты, кнопки тарифов, FloodWait)
tests/
├── fakes.py                            # FakeMarzban (с get_users), FakeBot/FakeNotifier, часы
├── conftest.py                         # фикстура файловой SQLite-БД и фабрики сессий
├── test_trial_rules.py
├── test_trial_activation.py
├── test_trial_notifications.py
└── test_trial_conversion.py
specs/002-free-trial/{spec,plan,research,data-model,quickstart,tasks}.md, contracts/, checklists/
```

## Потоки

### Выдача (`ActivateTrialUseCase.execute`)

1. `enabled=false` -> `DISABLED`. Per-user lock.
2. Есть запись: `active` -> `ALREADY_ACTIVE` (со ссылкой), `ended/converted` -> `ALREADY_USED`, `reserved` -> резюмируем п.6.
3. Есть `completed` или `pending` заказ -> `NOT_ELIGIBLE`.
4. Глобальный lock: `count(created_at >= now-24h) >= cap` -> `CAP_REACHED`; иначе `reserve` (INSERT, при `IntegrityError` - перечитать и ответить как для повтора).
5. (резерв сделан, lock отпущен)
6. Marzban: `get_user(name)`; 404 -> `get_inbounds` -> `build_inbounds_payload` -> `create_user(name, data_limit, expire, inbounds)`; иное исключение -> откат резерва -> `ERROR`.
7. Транзакция: `User` (get_or_create), `VPNProfile`, `Trial.status=active`, `granted_at`, `expires_at`. Сбой -> откат резерва -> `ERROR` (Marzban-пользователь остаётся и будет переиспользован).
8. `GRANTED(sub_url)`.

### Фоновая проверка (`TrialMonitorUseCase.run_once`, каждые 300 с)

1. Загрузить триалы `active` и `ended` с неотправленным напоминанием, не заблокированные.
2. Самолечение: у кого есть `completed` заказ - `mark_converted`, пропустить.
3. Пакетно `get_users` для `active` (порции по 50).
4. `evaluate_trial` (чистая): метки `reached_80_at` / `ended` (+причина), условные `UPDATE` (идемпотентно).
5. Очередь уведомлений (a)/(b)/(c) -> захват флага -> `notifier.send` -> `SENT` / `BLOCKED` (ставим флаг блокировки) / `FAILED` (снимаем флаг).
6. Если окно 80% пропущено (триал уже исчерпан) - (a) пропускается.

### Покупка триал-пользователя (`approve_order`)

В блоке вычисления плана `_apply_topup`: `get_user` (как раньше - проба существования и fallback на 404) -> `trial_repo.has_unconverted(user)` ? `build_trial_conversion_plan` : `calculate_renewal_plan`. Далее без изменений: сохранение плана, `reset`, `PUT` (`data_limit`, `expire`, `status=active`), затем `mark_converted`, затем `order=completed`, сообщение. Ветка `new` также вызывает `mark_converted` (идемпотентно).

## Ручные действия на боевой БД

Не требуются. Новая таблица `trials` создаётся автоматически при старте (`create_all`). Существующие таблицы не меняются. Опциональный ручной DDL - в [data-model.md](./data-model.md). В `.env` рекомендуется добавить 4 строки `TRIAL_*` (не обязательно - есть значения по умолчанию).

## Риски

1. **Marzban batch API** не проверен на живой панели (фильтр `username[]`, `limit`). Деградация: ошибка пакетного запроса -> лог, трафик не оценивается, срок проверяется по нашей БД.
2. **Задержка учёта трафика в Marzban** - 80% и «закончился по трафику» определяются с задержкой до интервала проверки + задержка панели.
3. **Один процесс**: per-user/global `asyncio.Lock` и суточный лимит рассчитаны на один экземпляр бота (как и сейчас). Уникальность триала гарантируется БД независимо от этого.
4. **Общая сессия приложения** (существующий дизайн) небезопасна для конкурентного использования; фон работает только через собственные сессии. Существующие репозитории не переписываем.
5. **Потеря уведомления** при падении между захватом флага и отправкой (осознанный выбор «не более одного раза»).
6. **Конкурирующий pending-заказ**: решено блокировкой выдачи триала при наличии `pending`.
7. **Telegram**: пользователи, заблокировавшие бота, помечаются и исключаются; FloodWait обрабатывается.

## Project Structure / Structure Decision

Один проект, строгое разделение слоёв (`domain` / `use_cases` / `adapters`), как в 001.

## Complexity Tracking

Нет нарушений конституции.
