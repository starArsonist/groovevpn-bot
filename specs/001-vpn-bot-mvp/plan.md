# Implementation Plan: vpn-bot-mvp

**Branch**: `[001-vpn-bot-mvp]` | **Date**: 2026-08-04 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/001-vpn-bot-mvp/spec.md`

## Summary

Разработка MVP Telegram-бота для продажи резидентского VPN (трафик-пакеты). Реализуется на Python с использованием Clean Architecture, SQLite (SQLAlchemy) и интеграцией с Marzban REST API для управления VPN-пользователями. Оплата подтверждается вручную администратором по скриншотам чеков.

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
