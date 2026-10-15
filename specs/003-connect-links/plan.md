# Implementation Plan: connect-links

**Branch**: `feature/connect-links` (от `dev`) | **Date**: 2026-10-06 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/003-connect-links/spec.md`

## Summary

Кнопки приложений («Подключить в Happ/V2RayTun/Hiddify») под ссылкой подписки. Bot API принимает в URL-кнопке только http(s) и tg://, поэтому кнопка ведёт на статичную https-страницу `web/connect/index.html`, которая валидирует фрагмент URL (приложение из белого списка, https-подписка с хостом из `ALLOWED_SUB_HOSTS`) и открывает deeplink приложения. Бот строит URL страницы чистой функцией, показывает кнопки при первой выдаче подписки (триал, первая покупка) и на экране «Моя подписка», а при пустой/невалидной конфигурации работает как раньше.

## Technical Context

**Language/Version**: Python 3.12+; страница - HTML + ванильный JS (один файл)  
**Primary Dependencies**: без новых зависимостей (Python: `python-telegram-bot`, `loguru` уже есть; для тестов страницы - встроенный `node:test`, Node v24 на машине, в зависимости проекта не добавляется)  
**Storage**: не используется, **схема БД не меняется**  
**Testing**: `pytest` + `pytest-asyncio` (существующие 86 тестов + новые); `node --test` для логики страницы (обёртка в pytest пропускается без Node)  
**Target Platform**: Docker (бот); любой статический https-хостинг (страница, деплой на владельце)  
**Project Type**: Telegram Bot (Service) + статическая страница  
**Constraints**: Clean Architecture + DI; async; строгая типизация; `loguru` без утечки URL подписки; линтеры не добавляем  
**Scale/Scope**: 3 приложения, 2 новые переменные окружения

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Clean Architecture**: ✅ Правила построения URL и валидация конфигурации - чистые функции в `domain`; сборка кнопок Telegram - адаптер `adapters/tg_bot`; use case получает готовый компонент.
- **II. DI**: ✅ `ConnectKeyboard` создаётся в `main.py` и передаётся в `AdminUseCases` (конструктор) и в хендлеры (`bot_data`); внутри хендлеров/use cases ничего не создаётся.
- **III. Async I/O First**: ✅ отправка/повтор сообщений - `async`.
- **IV. Строгая типизация**: ✅ type hints.
- **V. Логирование**: ✅ `loguru`: предупреждения конфигурации, факт показа кнопок; URL подписки и страницы с фрагментом не логируются (закрепляется тестом).
- Нарушений нет, `Complexity Tracking` пуст.

## Точки изменения существующего кода

| Файл | Изменение |
|------|-----------|
| `src/config.py` | + `connect_page_url: str = ""`, `connect_apps: str = "happ,v2raytun,hiddify"` |
| `.env.example` | + `CONNECT_PAGE_URL=`, `CONNECT_APPS=happ,v2raytun,hiddify` |
| `src/use_cases/admin_use_cases.py` | + необязательный `connect_keyboard=None`; в `_apply_new_purchase` кнопки приложений + «Поддержка», сокращённый текст при наличии кнопок, отправка через повтор без кнопок. `_apply_topup` (продление) **не меняется** |
| `src/adapters/tg_bot/handlers/trial.py` | `render_trial_result` принимает строки кнопок приложений (GRANTED и ALREADY_ACTIVE); хендлер берёт `connect_keyboard` из `bot_data`; отправка через повтор без кнопок |
| `src/adapters/tg_bot/handlers/subscription.py` | кнопки приложений над «Продлить / докупить»; **`html.escape` для ссылки** (сейчас отсутствует); повтор без кнопок |
| `src/main.py` | сборка `ConnectKeyboard` из настроек (с предупреждениями), DI в `AdminUseCases` и `bot_data` |

Не меняются: `calculate_renewal_plan`, триал-логика выдачи/уведомлений, `_apply_topup`, тексты продления, БД.

## Новые файлы

```text
src/
├── domain/connect_links.py            # SUPPORTED_APPS, ConnectConfig, parse_connect_config, build_connect_url (чистые)
└── adapters/tg_bot/connect.py         # ConnectKeyboard, build_connect_keyboard(settings), send/edit с повтором без кнопок
web/connect/
├── index.html                         # страница-прослойка (один файл, без внешних запросов)
├── validate.test.mjs                  # node:test: логика валидации + статические проверки файла
└── README.md                          # деплой: статика по https, X-Robots-Tag, Referrer-Policy, ALLOWED_SUB_HOSTS
tests/
├── test_connect_links.py              # чистые функции: URL страницы, валидация конфигурации
├── test_connect_ui.py                 # кнопки: триал, первая покупка, нет при продлении, экран подписки, экранирование, fallback, логи
└── test_connect_page.py               # обёртка: node --test (skip без Node)
specs/003-connect-links/{spec,plan,research,tasks,quickstart}.md, contracts/, checklists/
```

## Дизайн

### Чистые функции (domain/connect_links.py)

- `parse_connect_config(page_url, apps_csv) -> (ConnectConfig | None, warnings)`: https, хост с точкой, без учётных данных, пробелов; фрагмент отбрасывается; приложения - по запятой, без регистра и дублей, неизвестные пропускаются, порядок сохраняется. Пустое/невалидное -> `None` + предупреждения (текст без значений-секретов).
- `build_connect_url(config, app, sub_url) -> str`: `<страница>#app=<app>&sub=<quote(sub_url, safe="")>`.

### Страница (web/connect/index.html)

- `<head>`: `meta referrer no-referrer`, `meta robots noindex,nofollow`, CSP-мета `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'`, `color-scheme`.
- Константы в начале: `ALLOWED_SUB_HOSTS = []`, `PROFILE_NAME = 'Groove'`, `OPEN_DELAY_MS`.
- Логика (`LOGIC:BEGIN/END`): `APPS` (шаблоны схем + название + ссылка «Где скачать»), `parseFragment`, `validate(params, allowedHosts)`, `buildDeeplink(app, sub, profileName)`, `isAllowedDeeplink`.
- DOM: только `textContent`/`createElement`/`setAttribute`; крупная кнопка «Открыть в <приложении>» (`<a href=deeplink>`), «Скопировать ссылку подписки» (`navigator.clipboard`, запас - `textarea` + `execCommand('copy')`), подсказка (текст из задачи), блок ошибки, `<noscript>`. Автопереход через `setTimeout`.
- Тема: CSS-переменные + `prefers-color-scheme`.

### Бот

- `ConnectKeyboard.app_rows(sub_url)`: `[]`, если фича выключена, ссылка пустая или URL кнопки длиннее предела (с предупреждением без URL); иначе по строке на приложение, `InlineKeyboardButton("Подключить в Happ", url=...)`.
- Отправка с запасным вариантом: сначала сообщение с кнопками приложений; при `BadRequest` - то же сообщение без них (предупреждение в лог без URL).
- Тексты: с кнопками - короткая подсказка вместо инструкции Happ; без - прежний текст.
- Кнопки не показываются в `_topup_success_message` и в сообщениях о продлении.

## Ручные действия владельца

- На деплое: положить `web/connect/` на https-хостинг (как статику), заголовки `X-Robots-Tag: noindex` и `Referrer-Policy: no-referrer`, прописать хост подписки в `ALLOWED_SUB_HOSTS` в начале `index.html`, указать адрес страницы в `CONNECT_PAGE_URL` (https, хост с точкой).
- Проверить на устройствах: открытие приложений на iOS/Android/Windows/macOS, схемы Happ и V2RayTun, поведение во встроенном браузере Telegram.
- БД: **ничего** (схема не меняется).

## Риски

См. research.md (схемы Happ/V2RayTun не подтверждены официальной документацией; отказ Telegram по кнопкам; блокировка автоперехода; токен в истории браузера; пустой `ALLOWED_SUB_HOSTS` по умолчанию).

## Complexity Tracking

Нет нарушений конституции.
