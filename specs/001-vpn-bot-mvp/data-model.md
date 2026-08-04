# Data Model: Groove VPN Bot

## Основные сущности (SQLAlchemy SQLite)

### User (Пользователь Telegram)
Описывает клиента бота.
- `id`: BigInt, Primary Key (Telegram User ID)
- `username`: String, Nullable (Telegram username)
- `is_admin`: Boolean, Default = False
- `created_at`: DateTime, Default = func.now()

### Order (Заявка на покупку)
Заявка на покупку пакета трафика.
- `id`: Integer, Primary Key, Autoincrement
- `user_id`: BigInt, Foreign Key (User.id)
- `tariff_gb`: Integer (Объем трафика пакета в ГБ, например, 50, 150, 450)
- `status`: String (enum: `pending`, `completed`, `rejected`)
- `photo_file_id`: String (Telegram file ID скриншота чека)
- `created_at`: DateTime, Default = func.now()

### VPNProfile (Профиль в Marzban)
Связь между пользователем Telegram и созданным профилем в Marzban.
- `id`: Integer, Primary Key, Autoincrement
- `user_id`: BigInt, Foreign Key (User.id)
- `marzban_username`: String, Unique (Генерируется ботом, например, `user_{user_id}_{random}`)
- `sub_url`: String (Ссылка на подписку VLESS)
- `status`: String (enum: `active`, `disabled`)
- `created_at`: DateTime, Default = func.now()

## DTOs (Data Transfer Objects)
Используются для передачи данных между слоями.
- `OrderCreateDTO`: `user_id`, `tariff_gb`, `photo_file_id`
- `OrderApproveDTO`: `order_id`
- `MarzbanUserCreateDTO`: `username`, `proxies`, `data_limit`, `data_limit_reset_strategy`, `expire`
- `MarzbanUserResponseDTO`: `username`, `status`, `used_traffic`, `data_limit`, `subscription_url`
