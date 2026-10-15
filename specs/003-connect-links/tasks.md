# Tasks: connect-links

**Input**: Design documents from `/specs/003-connect-links/`
**Prerequisites**: plan.md, spec.md, research.md, contracts/
**Результат**: `uv run pytest -q` -> `148 passed` (86 прежних без изменений + 62 новых), `node --test web/connect/validate.test.mjs` -> 46 passed.
**Baseline**: до начала работ `uv run pytest -q` -> `86 passed` (зафиксировано 2026-10-06 на `feature/connect-links`, Node v24.14.0 доступен). После изменений - 86 + новые тесты зелёные, существующие тестовые файлы не редактируются.
**Ограничения**: без новых Python-зависимостей, без изменения схемы БД, без линтеров, коммиты/push только по команде владельца.

## Format: `[ID] [P?] [Story] Description`

## Phase 1: Setup

- [x] T001 `src/config.py`: `connect_page_url=""`, `connect_apps="happ,v2raytun,hiddify"`
- [x] T002 [P] `.env.example`: добавить `CONNECT_PAGE_URL=` и `CONNECT_APPS=happ,v2raytun,hiddify`

## Phase 2: Foundational

- [x] T003 `src/domain/connect_links.py`: `SUPPORTED_APPS`, `ConnectConfig`, `parse_connect_config`, `build_connect_url` (чистые функции)
- [x] T004 `src/adapters/tg_bot/connect.py`: `ConnectKeyboard` (`app_rows`), `build_connect_keyboard(page_url, apps_csv)` (валидация при старте + предупреждения loguru, не падает), отправка/редактирование с повтором без кнопок при `BadRequest`

## Phase 3: User Story 1 - Кнопки после выдачи (P1) MVP

### Tests (первыми)

- [x] T005 [P] [US1] `tests/test_connect_links.py`: кодирование (`&`, `?`, `#`, `+`, `%`, кириллица), фрагмент вместо query, порядок приложений, дедупликация, неизвестные приложения, невалидные адреса страницы (http, без точки в хосте, пробелы, учётные данные, пусто) -> `None` + предупреждения, фрагмент в адресе страницы отбрасывается
- [x] T006 [P] [US1] `tests/test_connect_ui.py`: кнопки при выдаче триала (GRANTED, ALREADY_ACTIVE) и первой покупки (включая fallback 404 -> новый пользователь); **нет кнопок при продлении** (обычное продление и покупка триал-пользователем); порядок: приложения, действия, «Поддержка»; HTML-экранирование ссылки (`<`, `>`, `&`) в триале, покупке и «Моя подписка»; пустой/невалидный `CONNECT_PAGE_URL` - кнопок нет, тексты прежние, не падает; отказ Telegram по клавиатуре -> повтор без кнопок, ссылка доставлена; URL подписки и страницы не попадают в логи (sink loguru)

### Implementation

- [x] T007 [US1] `src/adapters/tg_bot/handlers/trial.py`: кнопки в `render_trial_result` (GRANTED, ALREADY_ACTIVE), короткий текст при наличии кнопок, повтор без кнопок
- [x] T008 [US1] `src/use_cases/admin_use_cases.py`: необязательный `connect_keyboard`; кнопки и текст в `_apply_new_purchase`; продление без изменений

## Phase 4: User Story 2 - Страница-прослойка (P1)

### Tests (первыми)

- [x] T009 [P] [US2] `web/connect/validate.test.mjs` (`node:test`, без зависимостей; выполняет блок `LOGIC:BEGIN/END` из `index.html`): допустимый хост; чужой хост; поддомен/суффикс чужого хоста; хост в userinfo (`https://allowed@evil`); регистр хоста; неизвестное приложение; `__proto__`/`constructor`; пустой фрагмент; `javascript:`/`data:`/`http:`/`file:` в `sub`; пустой `ALLOWED_SUB_HOSTS`; `sub` с фрагментом/пробелами/управляющими символами/слишком длинный; сборка deeplink для трёх приложений (имя профиля Hiddify кодируется); итоговая схема из белого списка; статические проверки файла (нет `innerHTML` и т.п., нет внешних загрузок, `ALLOWED_SUB_HOSTS` пуст в репозитории, есть обе meta)
- [x] T010 [P] [US2] `tests/test_connect_page.py`: обёртка `node --test web/connect/validate.test.mjs` (skip без Node)

### Implementation

- [x] T011 [US2] `web/connect/index.html`: страница по плану (meta, CSP, константы, логика, DOM через `textContent`, кнопки «Открыть в ...» / «Скопировать ссылку подписки», подсказка, тема, `noscript`, ссылки «Где скачать» только для Happ и Hiddify)
- [x] T012 [P] [US2] `web/connect/README.md`: деплой (статика по https, `X-Robots-Tag: noindex`, `Referrer-Policy: no-referrer`), что прописать в `ALLOWED_SUB_HOSTS`, `PROFILE_NAME`, источники ссылок «Где скачать», чек-лист проверки на устройствах

## Phase 5: User Story 3 - «Моя подписка» (P2)

- [x] T013 [US3] `src/adapters/tg_bot/handlers/subscription.py`: кнопки приложений, `html.escape` ссылки, повтор без кнопок (тесты - T006)

## Phase 6: User Story 4 - Бот без страницы (P2)

- [x] T014 [US4] Проверить, что пустая/невалидная конфигурация даёт прежние тексты и клавиатуры (тесты - T005/T006)

## Phase 7: Polish & Assembly

- [x] T015 `src/main.py`: `build_connect_keyboard(settings.connect_page_url, settings.connect_apps)`, DI в `AdminUseCases(connect_keyboard=...)`, `bot_data["connect_keyboard"]`
- [x] T016 Прогнать весь набор: `uv run pytest -q` (86 прежних + новые) и `node --test web/connect/validate.test.mjs`; импорт `src.main`
- [x] T017 Отчёт: что изменено, что не проверено (реальное открытие приложений на iOS/Android/Windows/macOS, встроенный браузер Telegram, схемы Happ/V2RayTun)

## Dependencies & Execution Order

- Phase 1 -> Phase 2 -> Phase 3 (US1) и Phase 4 (US2) независимы друг от друга -> Phase 5/6 -> Phase 7.
- Тесты каждой истории пишутся до реализации и сначала падают.
