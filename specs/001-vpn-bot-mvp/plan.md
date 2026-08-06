# Implementation Plan: vpn-bot-mvp

**Branch**: `[001-vpn-bot-mvp]` | **Date**: 2026-08-04 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/001-vpn-bot-mvp/spec.md`

## Summary

Разработка MVP Telegram-бота для продажи резидентского VPN (трафик-пакеты). Реализуется на Python с использованием Clean Architecture, SQLite (SQLAlchemy) и интеграцией с Marzban REST API для управления VPN-пользователями. Оплата подтверждается вручную администратором по скриншотам чеков.

## 3. Business Logic (Use Cases):
- `CreateOrderUseCase`: Обработка создания заказа (сохранение в БД). Должен определять, является ли заказ первой покупкой (поиск по `VPNProfile`) или продлением. В зависимости от этого ставить маркер `order_type`.
- `ApproveOrderUseCase`: Если заказ на покупку: запрашивает Inbounds, генерирует username, делает POST `/api/user`, сохраняет `VPNProfile` в БД, и отправляет клиенту ссылку. Если заказ на продление: получает текущий лимит через GET `/api/user/{username}`, прибавляет купленный трафик, делает PUT `/api/user/{username}` с новым лимитом, и уведомляет клиента.
- `CheckTrafficUseCase`: Получение `used_traffic` и расчет оставшегося лимита.

## 2. Интеграция с Marzban API (Secondary Adapter):
- Создать асинхронный HTTP-клиент (на базе aiohttp или httpx) с учетом спецификации из `marzban_openapi.json`.
- Реализовать авторизацию OAuth2PasswordBearer через POST `/api/admin/token` с логином/паролем из `.env` и кэшированием токена в памяти.
- Метод создания пользователя (POST `/api/user`). `username` должен генерироваться в формате `user_{telegram_id}_{order_id}`.
- Метод получения Inbounds (GET `/api/inbounds`), извлекающий все протоколы и их `tag`, чтобы динамически формировать поле `inbounds` в payload при создании пользователя.
- Метод обновления пользователя (PUT `/api/user/{username}`) для увеличения `data_limit` при продлении.
- Метод проверки трафика (GET `/api/user/{username}`).

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: `python-telegram-bot` (v20+), `SQLAlchemy` (async), `httpx` (для API), `loguru`
**Storage**: SQLite (асинхронно через `aiosqlite`)
**Testing**: `pytest`, `pytest-asyncio`
**Target Platform**: Docker (docker-compose)
**Project Type**: Telegram Bot (Service)
**Performance Goals**: Поддержка 50+ одновременных юзеров без блокировки I/O
**Constraints**: Clean Architecture strict separation, Dependency Injection для адаптеров
**Scale/Scope**: MVP для ручной обработки платежей с последующим автоматическим созданием пользователей в Marzban
**Environment**: Обязательные переменные: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_ID`, `MARZBAN_API_URL`, `MARZBAN_USERNAME`, `MARZBAN_PASSWORD` (указываются в `.env`)

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **Clean Architecture Strictness**: ✅ Выполнено. Описаны слои `domain`, `use_cases`, `adapters`.
- **Dependency Injection**: ✅ Выполнено. `async_sessionmaker` и `MarzbanClient` прокидываются.
- **Async I/O First**: ✅ Выполнено. Везде `async`/`await`.
- **Строгая типизация**: ✅ Выполнено. Будут использованы `type hints`.
- **Подробное логирование**: ✅ Выполнено. `loguru`.

## Project Structure

### Documentation (this feature)

```text
specs/001-vpn-bot-mvp/
├── plan.md              # This file
├── research.md          # Architectural decisions & research
├── data-model.md        # DB Schema & Entities
├── quickstart.md        # How to run the bot locally
├── contracts/           # API integration contracts
└── tasks.md             # Tasks (To be generated)
```

### Source Code (repository root)

```text
src/
├── domain/              # Бизнес-модели (SQLAlchemy models, DTOs)
├── use_cases/           # Бизнес-логика (заказы, интеграция)
└── adapters/
    ├── tg_bot/          # python-telegram-bot хэндлеры
    ├── db/              # SQLAlchemy репозитории
    └── marzban/         # Secondary адаптер для REST API

tests/
├── integration/
└── unit/

Dockerfile
docker-compose.yml
pyproject.toml
.env
```

**Structure Decision**: Архитектура с одним проектом, но строгим разделением на слои (Hexagonal Architecture / Clean Architecture).

## Complexity Tracking

Нет нарушений конституции.
