# Research & Architectural Decisions

## Dependency Injection в python-telegram-bot
- **Decision**: Использовать `bot_data` (хранилище `Application.bot_data`) или `context.bot_data` для хранения зависимостей, таких как `async_sessionmaker` и `MarzbanClient`.
- **Rationale**: Это встроенный и безопасный способ шаринга глобальных объектов в `python-telegram-bot` без создания синглтонов. Адаптеры будут инициализироваться в точке входа (`main.py`) и прокидываться в хэндлеры.
- **Alternatives considered**: Использование сторонних DI-контейнеров (например, `dependency-injector` или `dishka`). Отвергнуто для MVP ради простоты, `bot_data` вполне достаточно.

## Интеграция с Marzban API (Авторизация)
- **Decision**: Использовать `httpx.AsyncClient` с механизмом автоматического рефреша токена.
- **Rationale**: `httpx` современный и удобный клиент. Класс `MarzbanClient` будет инкапсулировать состояние (токен). Перед каждым запросом (или при получении 401) будет вызываться метод получения токена через `/api/admin/token`.
- **Alternatives considered**: Использование `aiohttp`. `httpx` предоставляет более удобный API и встроенную поддержку HTTP/2 (по необходимости).

## Работа со скриншотами чеков
- **Decision**: Telegram-бот будет скачивать `file_id` полученного фото с наибольшим разрешением (`photo[-1].file_id`) и просто пересылать его (через `send_photo`) администратору.
- **Rationale**: Хранение самих бинарных данных фото в БД излишне, достаточно хранить `file_id`.
- **Alternatives considered**: Скачивание фото на диск. Отвергнуто для экономии места и усложнения логики.
