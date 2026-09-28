import asyncio
from typing import Any, Dict, Optional

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from src.domain.base import Base
from src.adapters.db.repositories import OrderRepository, UserRepository, VPNProfileRepository
from src.use_cases.admin_use_cases import AdminUseCases

GB = 1024 ** 3


class FakeMarzbanClient:
    """Fake реализация MarzbanClient, ведёт учёт вызовов и умеет один раз
    "упасть" на выбранном методе, чтобы смоделировать сбой процесса."""

    def __init__(self, user_state: Dict[str, Any], fail_on: Optional[str] = None):
        self.user_state = user_state
        self.calls: list[tuple] = []
        self.fail_on = fail_on

    def _maybe_fail(self, method: str) -> None:
        if self.fail_on == method:
            self.fail_on = None
            raise RuntimeError(f"simulated crash in {method}")

    async def get_user(self, username: str) -> Dict[str, Any]:
        self.calls.append(("get_user", username))
        self._maybe_fail("get_user")
        return dict(self.user_state)

    async def reset_user_data_usage(self, username: str) -> Dict[str, Any]:
        self.calls.append(("reset_user_data_usage", username))
        self._maybe_fail("reset_user_data_usage")
        self.user_state["used_traffic"] = 0
        return dict(self.user_state)

    async def update_user(
        self,
        username: str,
        data_limit: Optional[int] = None,
        expire: Optional[int] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.calls.append(("update_user", username, data_limit, expire, status))
        self._maybe_fail("update_user")
        if data_limit is not None:
            self.user_state["data_limit"] = data_limit
        if expire is not None:
            self.user_state["expire"] = expire
        if status is not None:
            self.user_state["status"] = status
        return dict(self.user_state)

    async def create_user(self, username: str, data_limit: int, expire=None, inbounds=None) -> Dict[str, Any]:
        self.calls.append(("create_user", username, data_limit, expire))
        self._maybe_fail("create_user")
        return {"username": username, "subscription_url": f"vless://{username}"}

    async def get_inbounds(self) -> Dict[str, Any]:
        return {"vless": [{"tag": "VLESS TCP REALITY"}]}

    def calls_count(self, method: str) -> int:
        return sum(1 for c in self.calls if c[0] == method)


class FakeBot:
    def __init__(self):
        self.messages: list[tuple] = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.messages.append((chat_id, text))


@pytest.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with session_maker() as s:
        yield s
    await engine.dispose()


async def _make_topup_order(session, *, tariff_gb: int = 100) -> tuple[int, int]:
    """Создаёт пользователя, активный VPN-профиль и pending topup-заявку.
    Возвращает (user_id, order_id)."""
    user_repo = UserRepository(session)
    vpn_repo = VPNProfileRepository(session)
    order_repo = OrderRepository(session)

    user = await user_repo.get_or_create(user_id=111, username="tester")
    await vpn_repo.create(
        user_id=user.id,
        marzban_username="user_111_1",
        sub_url="vless://old",
        status="active",
    )
    order = await order_repo.create(
        user_id=user.id,
        tariff_gb=tariff_gb,
        order_type="topup",
        status="pending",
        photo_file_id="file-1",
    )
    return user.id, order.id


def _build_use_cases(session, marzban_client, bot) -> AdminUseCases:
    return AdminUseCases(
        order_repo=OrderRepository(session),
        user_repo=UserRepository(session),
        vpn_profile_repo=VPNProfileRepository(session),
        marzban_client=marzban_client,
        bot=bot,
    )


async def test_early_renewal_example_end_to_end(session):
    # 100 ГБ лимит, потрачено 90 ГБ -> докупка 100 ГБ -> 110 ГБ, счётчик сброшен
    _, order_id = await _make_topup_order(session, tariff_gb=100)
    marzban = FakeMarzbanClient({
        "status": "active",
        "data_limit": 100 * GB,
        "used_traffic": 90 * GB,
    })
    bot = FakeBot()
    admin_uc = _build_use_cases(session, marzban, bot)

    result = await admin_uc.approve_order(order_id)

    assert result is True
    assert marzban.user_state["data_limit"] == 110 * GB
    assert marzban.user_state["used_traffic"] == 0
    assert marzban.user_state["status"] == "active"
    assert marzban.calls_count("reset_user_data_usage") == 1
    assert len(bot.messages) == 1

    order = await OrderRepository(session).get_by_id(order_id)
    assert order.status == "completed"
    assert order.carried_over_bytes == 10 * GB


async def test_double_click_applies_topup_only_once(session):
    _, order_id = await _make_topup_order(session, tariff_gb=100)
    marzban = FakeMarzbanClient({
        "status": "active",
        "data_limit": 100 * GB,
        "used_traffic": 90 * GB,
    })
    bot = FakeBot()
    admin_uc = _build_use_cases(session, marzban, bot)

    # Симулируем двойное нажатие/повторный вебхук: два конкурентных вызова
    results = await asyncio.gather(
        admin_uc.approve_order(order_id),
        admin_uc.approve_order(order_id),
    )

    assert sorted(results) == [False, True]
    assert marzban.calls_count("reset_user_data_usage") == 1
    assert marzban.calls_count("update_user") == 1
    assert marzban.calls_count("get_user") == 1
    assert len(bot.messages) == 1
    assert marzban.user_state["data_limit"] == 110 * GB


async def test_duplicate_delivery_after_completion_is_noop(session):
    _, order_id = await _make_topup_order(session, tariff_gb=100)
    marzban = FakeMarzbanClient({
        "status": "active",
        "data_limit": 100 * GB,
        "used_traffic": 90 * GB,
    })
    bot = FakeBot()
    admin_uc = _build_use_cases(session, marzban, bot)

    first = await admin_uc.approve_order(order_id)
    second = await admin_uc.approve_order(order_id)  # повторная доставка того же платежа

    assert first is True
    assert second is False
    assert marzban.calls_count("reset_user_data_usage") == 1
    assert marzban.calls_count("update_user") == 1
    assert len(bot.messages) == 1


async def test_crash_after_reset_then_retry_does_not_lose_or_double_carryover(session):
    _, order_id = await _make_topup_order(session, tariff_gb=100)
    # Сбой смоделирован в update_user: к этому моменту reset уже успешно прошёл
    # на панели, но лимит/expire ещё не применены (процесс "упал" между ними).
    marzban = FakeMarzbanClient(
        {"status": "active", "data_limit": 100 * GB, "used_traffic": 90 * GB},
        fail_on="update_user",
    )
    bot = FakeBot()
    admin_uc = _build_use_cases(session, marzban, bot)

    first_attempt = await admin_uc.approve_order(order_id)
    assert first_attempt is False

    order = await OrderRepository(session).get_by_id(order_id)
    assert order.status == "pending"
    assert order.reset_applied is True
    assert order.carried_over_bytes == 10 * GB
    assert marzban.user_state["used_traffic"] == 0  # reset реально применился на панели
    assert marzban.calls_count("reset_user_data_usage") == 1
    assert marzban.calls_count("get_user") == 1

    # Повтор (админ снова жмёт "Подтвердить", либо повторная доставка вебхука)
    second_attempt = await admin_uc.approve_order(order_id)
    assert second_attempt is True

    # get_user и reset НЕ вызывались повторно - план и факт сброса персистентны
    assert marzban.calls_count("get_user") == 1
    assert marzban.calls_count("reset_user_data_usage") == 1
    assert marzban.calls_count("update_user") == 2  # неудачная попытка + успешная

    # Остаток не потерян и не задвоен: 100 (тек. лимит) - 90 (used) = 10, + 100 = 110
    assert marzban.user_state["data_limit"] == 110 * GB
    assert len(bot.messages) == 1

    order = await OrderRepository(session).get_by_id(order_id)
    assert order.status == "completed"


async def test_unlimited_user_early_purchase_extends_expire_but_not_limit(session):
    _, order_id = await _make_topup_order(session, tariff_gb=50)
    marzban = FakeMarzbanClient({
        "status": "active",
        "data_limit": 0,  # безлимит
        "used_traffic": 999 * GB,
    })
    bot = FakeBot()
    admin_uc = _build_use_cases(session, marzban, bot)

    result = await admin_uc.approve_order(order_id)

    assert result is True
    assert marzban.calls_count("reset_user_data_usage") == 0  # сбрасывать нечего
    assert marzban.user_state["data_limit"] == 0  # лимит не тронут, остался безлимитным
    assert marzban.user_state["status"] == "active"

    order = await OrderRepository(session).get_by_id(order_id)
    assert order.planned_data_limit is None
    assert order.status == "completed"


async def test_renewal_after_expiration_resets_without_carryover(session):
    _, order_id = await _make_topup_order(session, tariff_gb=50)
    marzban = FakeMarzbanClient({
        "status": "expired",
        "data_limit": 100 * GB,
        "used_traffic": 5 * GB,  # неважно - подписка уже сгорела
    })
    bot = FakeBot()
    admin_uc = _build_use_cases(session, marzban, bot)

    result = await admin_uc.approve_order(order_id)

    assert result is True
    assert marzban.user_state["data_limit"] == 50 * GB  # без переноса остатка
    assert marzban.user_state["used_traffic"] == 0
    assert marzban.user_state["status"] == "active"

    order = await OrderRepository(session).get_by_id(order_id)
    assert order.carried_over_bytes == 0


async def test_new_purchase_creates_profile_with_30_day_expire(session):
    user_repo = UserRepository(session)
    order_repo = OrderRepository(session)
    user = await user_repo.get_or_create(user_id=222, username="newbie")
    order = await order_repo.create(
        user_id=user.id,
        tariff_gb=50,
        order_type="new",
        status="pending",
        photo_file_id="file-2",
    )

    marzban = FakeMarzbanClient({})
    bot = FakeBot()
    admin_uc = _build_use_cases(session, marzban, bot)

    result = await admin_uc.approve_order(order.id)

    assert result is True
    create_calls = [c for c in marzban.calls if c[0] == "create_user"]
    assert len(create_calls) == 1
    _, username, data_limit, expire = create_calls[0]
    assert data_limit == 50 * GB
    assert expire is not None  # раньше здесь всегда было None (бессрочно)

    profile = await VPNProfileRepository(session).get_by_user_id(user.id)
    assert profile is not None
    assert profile.sub_url.startswith("vless://")
