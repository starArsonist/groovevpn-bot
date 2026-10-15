# Tasks: free-trial

**Input**: Design documents from `/specs/002-free-trial/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/
**Результат**: `uv run pytest -q` -> `86 passed` (13 существующих без изменений + 73 новых), 5 прогонов подряд стабильно.
**Baseline**: до начала работ `uv run pytest -q` -> `13 passed` (зафиксировано 2026-10-06 на `feature/trial`). После изменений - все 13 + новые тесты зелёные, существующие тестовые файлы не редактируются.
**Ограничения**: без новых зависимостей, без линтеров, без изменения существующих таблиц, коммиты/push только по команде владельца.

## Format: `[ID] [P?] [Story] Description`

## Phase 1: Setup

- [x] T001 `src/config.py`: добавить `trial_enabled`, `trial_data_gb`, `trial_days`, `trial_daily_cap` с дефолтами (true/10/7/50)
- [x] T002 [P] `.env.example`: добавить `TRIAL_ENABLED=true`, `TRIAL_DATA_GB=10`, `TRIAL_DAYS=7`, `TRIAL_DAILY_CAP=50`
- [x] T003 [P] `tests/conftest.py` + `tests/fakes.py`: фикстура файловой временной SQLite + `async_sessionmaker`, `FakeMarzban` (create/get/get_users/update/reset/inbounds, управляемые сбои, счётчики вызовов), `FakeNotifier`, управляемые часы

## Phase 2: Foundational (блокирует все истории)

- [x] T004 `src/domain/models/trial.py` + экспорт в `src/domain/models/__init__.py`: модель `Trial` (см. data-model.md)
- [x] T005 [P] `src/domain/trial_rules.py`: `TrialConfig`, `evaluate_trial` (чистая), `build_trial_conversion_plan` (возвращает `RenewalPlan` с carried_over_bytes=0)
- [x] T006 [P] `src/adapters/marzban/inbounds.py`: `build_inbounds_payload`; `approve_order._apply_new_purchase` использует её (чистое извлечение, поведение не меняется)
- [x] T007 [P] `src/adapters/marzban/client.py`: `get_users(usernames)` - пакетный `GET /api/users` порциями
- [x] T008 `src/adapters/db/trial_repository.py`: `TrialRepository` (session factory): `get`, `reserve`, `release`, `activate` (User + VPNProfile + status в одной транзакции), `count_created_since`, `has_paid_order`, `has_pending_order`, `has_unconverted`, `mark_converted`, `list_monitored`, `mark_reached_low`, `mark_ended`, `claim_notification`, `release_notification`, `mark_blocked`
- [x] T009 [P] `src/adapters/scheduler.py`: `run_periodically(name, interval, job)` (ошибки логируются, цикл живёт, `CancelledError` пробрасывается)

## Phase 3: User Story 1 - Получить пробный период (P1) MVP

**Independent Test**: фейковый Marzban + временная БД, нажатие от нового telegram_id -> запись `active`, профиль, пользователь Marzban, ссылка.

### Tests (написать первыми)

- [x] T010 [P] [US1] `tests/test_trial_rules.py`: `evaluate_trial` (80%, трафик исчерпан, срок вышел, `limited`/`expired`, пропущенное окно 80%, нет данных Marzban), `build_trial_conversion_plan`
- [x] T011 [P] [US1] `tests/test_trial_activation.py`: новый пользователь получает триал; повторное нажатие и повторный `/start` не дают второго; пользователь с подтверждённым платным заказом - нет; pending-заказ - нет; суточный лимит; `TRIAL_ENABLED=false`; сбой Marzban -> резерв откатывается и повтор без дубликата; пользователь уже есть в Marzban -> переиспользуется; параллельное двойное нажатие (`asyncio.gather`); уникальность на уровне БД без in-process lock (два use case на одну БД)

### Implementation

- [x] T012 [US1] `src/use_cases/trial_use_cases.py`: `ActivateTrialUseCase`, `TrialOfferUseCase` (+ `TrialActivationResult`)
- [x] T013 [US1] `src/adapters/tg_bot/handlers/trial.py`: обработчик `trial_start` (тексты результатов, кнопки: «Моя подписка»/«Купить VPN»/«Поддержка»)
- [x] T014 [US1] `src/adapters/tg_bot/handlers/start.py`: кнопка «Попробовать бесплатно» первой, если `TrialOfferUseCase` разрешает (ошибка проверки не ломает `/start`)

**Checkpoint**: US1 полностью работает и тестируется независимо.

## Phase 4: User Story 2 - Уведомления (P1)

### Tests

- [x] T015 [P] [US2] `tests/test_trial_notifications.py`: расход до 80% - одно уведомление, повторные проверки не дублируют; закончился по трафику - одно; закончился по сроку - одно; напоминание через 24 ч - одно; перезапуск (новый экземпляр use case на той же БД) не даёт дублей; блокировка бота - пометка и больше нет попыток; купил пакет - уведомлений нет; пропущенное окно 80%; временная ошибка отправки - флаг снимается, повтор; `RetryAfter` обрабатывается; пакетность (число вызовов `get_users` не растёт с числом пользователей)

### Implementation

- [x] T016 [US2] `src/use_cases/trial_monitor.py`: порт `TrialNotifier`, `TrialMonitorUseCase.run_once`
- [x] T017 [US2] `src/adapters/tg_bot/notifier.py`: `TelegramTrialNotifier` (тексты, кнопки тарифов `tariff_{gb}` + «Поддержка», пауза между сообщениями, `RetryAfter`, `Forbidden`)

## Phase 5: User Story 3 - Покупка триал-пользователем (P1)

### Tests

- [x] T018 [P] [US3] `tests/test_trial_conversion.py`: `approve_order` для триал-пользователя - остаток триала не переносится, пользователь обновляется (не создаётся), `status=active`, триал -> `converted` с меткой; повторный `approve_order` того же заказа ничего не меняет; сбой после reset и повтор; триал закончился (`expired`/`limited`) - то же; два заказа подряд (первый конвертирует, второй платный перенос); пользователь удалён в Marzban (404) - fallback + `converted`; платный перенос без триала работает как прежде

### Implementation

- [x] T019 [US3] `src/use_cases/admin_use_cases.py`: необязательный `trial_repo`; ветка плана для триала в `_apply_topup`; `mark_converted` перед `completed` в обеих ветках; строка «Перенесено» скрывается при 0
- [x] T020 [US3] (по желанию) `src/adapters/tg_bot/handlers/payment.py`: пометка «после пробного периода» в уведомлении админу

## Phase 6: User Story 4 - Ограничения и отключение (P2)

- [x] T021 [US4] Проверить, что `TRIAL_ENABLED=false` скрывает кнопку и блокирует выдачу, но монитор продолжает обслуживать уже выданные триалы; откат резерва не входит в суточный лимит (покрыто T011/T015)

## Phase 7: Polish & Assembly

- [x] T022 `src/main.py`: DI (`TrialRepository`, use cases, notifier, `TrialConfig` из `Settings`), регистрация хендлера, запуск/остановка фоновой задачи, `bot_data` ключи
- [x] T023 Прогнать весь набор: `uv run pytest -q` (13 существующих + новые), импорт `src.main`
- [x] T024 Отчёт: что изменено, новая таблица `trials`, ручные шаги на боевой БД (нет, кроме опциональных), что не проверено на живой панели/в живом Telegram

## Dependencies & Execution Order

- Phase 1 -> Phase 2 -> Phase 3 (US1) -> Phase 4 (US2, использует T008/T016) / Phase 5 (US3, использует T008) -> Phase 7.
- US2 и US3 независимы друг от друга после Phase 2.
- Тесты каждой истории пишутся до реализации и сначала падают.
