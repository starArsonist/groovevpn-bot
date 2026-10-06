# Контракты: free-trial

## 1. Marzban API (добавляется один вызов)

Основано на `docs/marzban_openapi.json` (operationId `get_users`).

- **Endpoint**: `GET /api/users`
- **Query**: `username` (повторяющийся параметр, array of string), `limit` (integer), `offset` (integer)
- **Пример**: `GET /api/users?username=user_1_trial&username=user_2_trial&limit=50`
- **Response 200**: `{"users": [UserResponse...], "total": N}`; используемые поля: `username`, `status` (`active|disabled|limited|expired|on_hold`), `used_traffic` (байты), `data_limit` (байты), `expire` (unix-секунды или null).
- Клиент: `MarzbanClient.get_users(usernames: list[str]) -> list[dict]` - порции по 50 имён, возвращает объединённый список `users`.

Используемые уже существующие вызовы: `GET /api/inbounds`, `GET /api/user/{username}` (проба существования: 404 -> создавать), `POST /api/user` (`data_limit`, `expire`, `inbounds`, `status: active`), `POST /api/user/{username}/reset`, `PUT /api/user/{username}`.

## 2. Порт уведомлений (use_cases -> adapters)

`TrialNotificationKind` и `SendOutcome` определены в `src/domain/trial_rules.py`, протокол `TrialNotifier` - в `src/use_cases/trial_monitor.py`.

```python
class TrialNotificationKind(StrEnum):
    LOW = "low"            # (a) >= 80% трафика
    ENDED = "ended"        # (b) триал закончился
    REMINDER = "reminder"  # (c) напоминание через 24 ч

class SendOutcome(StrEnum):
    SENT = "sent"
    BLOCKED = "blocked"    # Forbidden: пользователь заблокировал бота
    FAILED = "failed"      # временная ошибка, можно повторить позже

class TrialNotifier(Protocol):
    async def send(self, telegram_id: int, kind: TrialNotificationKind, *,
                   used_bytes: int | None, limit_bytes: int, ended_reason: str | None) -> SendOutcome: ...
```

Реализация `TelegramTrialNotifier`: пауза ~50 мс между сообщениями, `RetryAfter` -> `sleep(retry_after + 1)` и до 3 повторов, `Forbidden` -> `BLOCKED`, прочие `TelegramError` -> `FAILED`.

## 3. Telegram UI

| Элемент | callback_data / URL | Поведение |
|---------|---------------------|-----------|
| Кнопка «Попробовать бесплатно» (первая на стартовом экране) | `trial_start` | Выдача триала (идемпотентна) |
| Кнопки тарифов в уведомлениях и ответах | `tariff_{gb}` (50/150/450) | Существующий `payment_conv_handler` (`pattern="^tariff_"`) |
| «Моя подписка», «Купить VPN», «Назад» | `my_subscription`, `buy_vpn`, `start` | Существующие |
| «Поддержка» | URL `https://t.me/starArsonist` | Существующая кнопка |

Тексты на русском, HTML, минимум эмодзи, короткий дефис. Черновики:

- Выдан: «Пробный период активирован. Доступно: 10 ГБ на 7 дней, действует до dd.mm.yyyy. Ссылка для подключения: ... Как добавить в приложение Happ: 1-4.»
- Уже выдан/активен: «Пробный период уже выдан.» (+ ссылка и «Моя подписка», если активен)
- Недоступен (лимит/выключен/уже платный): «Пробный период временно недоступен. Вы можете сразу выбрать пакет.» + кнопка «Купить VPN»
- Ошибка: «Не удалось активировать пробный период. Попробуйте ещё раз чуть позже.» + «Поддержка»
- (a): «Пробный трафик почти закончился: использовано N из M ГБ. Чтобы не потерять доступ, выберите пакет:» + тарифы
- (b): «Пробный период закончился (исчерпан трафик / истёк срок). Чтобы продолжить пользоваться VPN, выберите пакет:» + тарифы
- (c): «Напоминаем: пробный период закончился вчера. Выберите пакет, чтобы вернуть доступ:» + тарифы
