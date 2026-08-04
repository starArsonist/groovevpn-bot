# Контракт интеграции с Marzban API

Основан на спецификации `docs/marzban_openapi.json`.

## 1. Авторизация (Admin Token)
- **Endpoint**: `POST /api/admin/token`
- **Content-Type**: `application/x-www-form-urlencoded`
- **Payload**:
  - `username`: (из `.env` `MARZBAN_USERNAME`)
  - `password`: (из `.env` `MARZBAN_PASSWORD`)
- **Response**:
  - `200 OK`: `{"access_token": "string", "token_type": "bearer"}`

## 2. Создание пользователя
- **Endpoint**: `POST /api/user`
- **Headers**: `Authorization: Bearer <access_token>`
- **Content-Type**: `application/json`
- **Payload** (согласно OpenAPI `UserCreate`):
  ```json
  {
    "username": "user_123456789_xyz",
    "proxies": {"vless": {}},
    "data_limit": 53687091200, 
    "expire": null,
    "data_limit_reset_strategy": "no_reset",
    "status": "active"
  }
  ```
- **Response**:
  - `200 OK`: Данные созданного пользователя (включает `subscription_url`).

## 3. Получение информации о пользователе (Проверка трафика)
- **Endpoint**: `GET /api/user/{username}`
- **Headers**: `Authorization: Bearer <access_token>`
- **Response**:
  - `200 OK`: 
  ```json
  {
    "username": "string",
    "status": "active",
    "used_traffic": 1024,
    "data_limit": 53687091200,
    "subscription_url": "string"
  }
  ```
