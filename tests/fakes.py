import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.adapters.db.repositories import OrderRepository, UserRepository
from src.adapters.db.trial_repository import TrialRepository
from src.domain.trial_rules import SendOutcome, TrialConfig, TrialNotificationKind
from src.use_cases.trial_monitor import TrialMonitorUseCase
from src.use_cases.trial_use_cases import ActivateTrialUseCase, TrialActivationResult

GB = 1024 ** 3
START = datetime(2026, 10, 6, 12, 0, 0)


def http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://marzban.test")
    return httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)
    )


class FakeClock:
    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class FakeMarzban:
    """Фейковый клиент Marzban: хранит пользователей в памяти, считает вызовы,
    умеет один/несколько раз "упасть" на выбранном методе и притормаживать создание."""

    def __init__(self, delay: float = 0.0) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple] = []
        self.fail: dict[str, int] = {}
        self.delay = delay
        self.sub_url: str | None = None  # переопределить ссылку подписки новых пользователей

    def count(self, method: str) -> int:
        return sum(1 for call in self.calls if call[0] == method)

    def set_used(self, username: str, used_bytes: int) -> None:
        self.users[username]["used_traffic"] = used_bytes

    def set_status(self, username: str, status: str) -> None:
        self.users[username]["status"] = status

    def _enter(self, method: str, *args: Any) -> None:
        self.calls.append((method, *args))
        if self.fail.get(method, 0) > 0:
            self.fail[method] -= 1
            raise RuntimeError(f"simulated {method} failure")

    async def get_user(self, username: str) -> dict[str, Any]:
        self._enter("get_user", username)
        if username not in self.users:
            raise http_error(404)
        return dict(self.users[username])

    async def get_users(self, usernames: list[str]) -> list[dict[str, Any]]:
        self._enter("get_users", tuple(usernames))
        return [dict(self.users[name]) for name in usernames if name in self.users]

    async def get_inbounds(self) -> dict[str, Any]:
        self.calls.append(("get_inbounds",))
        return {"vless": [{"tag": "VLESS TCP REALITY"}]}

    async def create_user(
        self,
        username: str,
        data_limit: int,
        expire: Optional[int] = None,
        inbounds: Optional[dict[str, list[str]]] = None,
    ) -> dict[str, Any]:
        self._enter("create_user", username, data_limit, expire)
        if self.delay:
            await asyncio.sleep(self.delay)
        self.users[username] = {
            "username": username,
            "status": "active",
            "data_limit": data_limit,
            "used_traffic": 0,
            "expire": expire,
            "subscription_url": self.sub_url or f"https://sub.test/{username}",
        }
        return dict(self.users[username])

    async def reset_user_data_usage(self, username: str) -> dict[str, Any]:
        self._enter("reset_user_data_usage", username)
        self.users[username]["used_traffic"] = 0
        return dict(self.users[username])

    async def update_user(
        self,
        username: str,
        data_limit: Optional[int] = None,
        expire: Optional[int] = None,
        status: Optional[str] = None,
    ) -> dict[str, Any]:
        self._enter("update_user", username, data_limit, expire, status)
        user = self.users[username]
        if data_limit is not None:
            user["data_limit"] = data_limit
        if expire is not None:
            user["expire"] = expire
        if status is not None:
            user["status"] = status
        return dict(user)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, TrialNotificationKind]] = []
        self.attempts = 0
        self.outcomes: dict[int, list[SendOutcome]] = {}

    async def send(
        self,
        telegram_id: int,
        kind: TrialNotificationKind,
        *,
        used_bytes: int | None,
        limit_bytes: int,
        ended_reason: str | None,
    ) -> SendOutcome:
        self.attempts += 1
        queue = self.outcomes.get(telegram_id)
        outcome = queue.pop(0) if queue else SendOutcome.SENT
        if outcome == SendOutcome.SENT:
            self.sent.append((telegram_id, kind))
        return outcome

    def kinds_for(self, telegram_id: int) -> list[TrialNotificationKind]:
        return [kind for uid, kind in self.sent if uid == telegram_id]


class FakeBot:
    """Фейковый Telegram-бот: помнит сообщения и клавиатуры, умеет "отклонить" клавиатуру."""

    def __init__(self, reject_if: Any = None) -> None:
        self.messages: list[tuple[int, str]] = []
        self.calls: list[dict[str, Any]] = []
        self.reject_if = reject_if  # callable(reply_markup) -> bool: Telegram отклоняет такую клавиатуру

    async def send_message(self, chat_id: int, text: str, parse_mode: str | None = None, reply_markup: Any = None) -> None:
        if reply_markup is not None and self.reject_if is not None and self.reject_if(reply_markup):
            from telegram.error import BadRequest

            raise BadRequest("Button_url_invalid")
        self.messages.append((chat_id, text))
        self.calls.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})


def make_config(enabled: bool = True, data_gb: int = 10, days: int = 7, daily_cap: int = 50) -> TrialConfig:
    return TrialConfig(enabled=enabled, data_gb=data_gb, days=days, daily_cap=daily_cap)


@dataclass
class TrialEnv:
    session_factory: async_sessionmaker[AsyncSession]
    clock: FakeClock
    marzban: FakeMarzban
    repo: TrialRepository
    activate: ActivateTrialUseCase
    notifier: FakeNotifier
    monitor: TrialMonitorUseCase

    def new_activate(self, **config: Any) -> ActivateTrialUseCase:
        """Новый экземпляр use case (другие lock-и) на той же БД - модель "второго процесса"."""
        return ActivateTrialUseCase(self.repo, self.marzban, make_config(**config), clock=self.clock)

    def new_monitor(self, notifier: FakeNotifier | None = None) -> TrialMonitorUseCase:
        """Новый монитор (после "перезапуска бота") на той же БД."""
        return TrialMonitorUseCase(
            self.repo, self.marzban, notifier or self.notifier, clock=self.clock
        )

    async def grant(self, user_id: int) -> TrialActivationResult:
        return await self.activate.execute(user_id, f"user{user_id}")

    async def add_order(self, user_id: int, status: str, order_type: str = "new", tariff_gb: int = 50) -> int:
        async with self.session_factory() as session:
            await UserRepository(session).get_or_create(user_id=user_id, username=None)
            order = await OrderRepository(session).create(
                user_id=user_id,
                tariff_gb=tariff_gb,
                order_type=order_type,
                status=status,
                photo_file_id="photo",
            )
            return order.id


def build_env(session_factory: async_sessionmaker[AsyncSession], **config: Any) -> TrialEnv:
    clock = FakeClock()
    marzban = FakeMarzban()
    repo = TrialRepository(session_factory)
    notifier = FakeNotifier()
    return TrialEnv(
        session_factory=session_factory,
        clock=clock,
        marzban=marzban,
        repo=repo,
        activate=ActivateTrialUseCase(repo, marzban, make_config(**config), clock=clock),
        notifier=notifier,
        monitor=TrialMonitorUseCase(repo, marzban, notifier, clock=clock),
    )
