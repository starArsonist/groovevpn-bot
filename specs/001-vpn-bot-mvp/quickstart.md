# Quickstart

## Требования
- Docker и Docker Compose
- `uv` (опционально для локальной разработки)
- Python 3.12+

## Установка и запуск

1. Склонируйте репозиторий.
2. Скопируйте `.env.example` в `.env` и заполните данные:
   ```bash
   cp .env.example .env
   ```
   Укажите:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_ADMIN_ID`
   - `MARZBAN_API_URL`
   - `MARZBAN_USERNAME`
   - `MARZBAN_PASSWORD`
3. Запустите проект через Docker:
   ```bash
   docker-compose up -d --build
   ```
4. Бот автоматически создаст SQLite базу данных в примонтированном volume и запустится.

## Локальная разработка (с `uv`)

1. Установите зависимости:
   ```bash
   uv sync
   ```
2. Примените миграции (если используется alembic, иначе таблицы создадутся при старте бота).
3. Запустите бота:
   ```bash
   uv run python src/main.py
   ```
