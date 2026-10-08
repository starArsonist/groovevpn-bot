from datetime import datetime, timedelta, timezone

import pytest

import src.use_cases.admin_use_cases as admin_module
from src.adapters.db.repositories import OrderRepository, UserRepository, VPNProfileRepository
from src.domain.models import Order, VPNProfile
from src.use_cases.admin_use_cases import AdminUseCases
from src.use_cases.order_use_cases import CreateOrderUseCase
from tests.fakes import GB, FakeBot
from sqlalchemy import select

USER = 111
OTHER = 222


@pytest.fixture
async def shared_session(session_factory):
    async with session_factory() as session:
        yield session


@pytest.fixture
def ticking_clock(monkeypatch):
    """Каждое обращение к purchase_time_now() возвращает момент на час позже предыдущего."""
    state = {"now": datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)}

    def tick() -> datetime:
        state["now"] += timedelta(hours=1)
        return state["now"]

    monkeypatch.setattr(admin_module, "purchase_time_now", tick)


def _admin(shared_session, env, bot=None) -> AdminUseCases:
    return AdminUseCases(
        OrderRepository(shared_session),
        UserRepository(shared_session),
        VPNProfileRepository(shared_session),
        env.marzban,
        bot or FakeBot(),
        trial_repo=env.repo,
    )


async def _create_order(shared_session, tariff_gb: int = 50):
    uc = CreateOrderUseCase(
        OrderRepository(shared_session), UserRepository(shared_session), VPNProfileRepository(shared_session)
    )
    return await uc.execute(USER, "tester", tariff_gb, "photo")


async def _order(env, order_id: int) -> Order:
    async with env.session_factory() as session:
        return await session.get(Order, order_id)


async def _profiles(env, name: str) -> list[VPNProfile]:
    async with env.session_factory() as session:
        return list((await session.execute(select(VPNProfile).where(VPNProfile.marzban_username == name))).scalars())


async def test_plan_is_saved_before_marzban_and_reused_on_retry(env, shared_session, ticking_clock):
    order = await _create_order(shared_session)
    env.marzban.fail["create_user"] = 1
    admin = _admin(shared_session, env)

    assert await admin.approve_order(order.id) is False

    saved = await _order(env, order.id)
    assert saved.status == "pending"
    assert saved.plan_computed is True
    assert saved.planned_data_limit == 50 * GB
    assert saved.planned_expire_at is not None
    assert saved.carried_over_bytes == 0

    assert await admin.approve_order(order.id) is True

    create_calls = [c for c in env.marzban.calls if c[0] == "create_user"]
    assert len(create_calls) == 2
    assert create_calls[0][2:] == create_calls[1][2:]  # тот же лимит и тот же срок
    assert create_calls[0][3] == saved.planned_expire_at


async def test_conflict_on_create_reuses_own_marzban_user_and_applies_saved_plan(env, shared_session):
    order = await _create_order(shared_session)
    name = f"user_{USER}_{order.id}"
    # Предыдущая попытка создала пользователя в Marzban, но упала до записи профиля
    await env.marzban.create_user(name, 1 * GB, expire=5)
    bot = FakeBot()

    assert await _admin(shared_session, env, bot).approve_order(order.id) is True

    saved = await _order(env, order.id)
    assert saved.status == "completed"
    user = env.marzban.users[name]
    assert user["data_limit"] == 50 * GB
    assert user["expire"] == saved.planned_expire_at
    assert user["status"] == "active"
    profiles = await _profiles(env, name)
    assert len(profiles) == 1 and profiles[0].user_id == USER
    assert profiles[0].sub_url == user["subscription_url"]
    assert len(bot.messages) == 1


async def test_existing_own_profile_is_reused_without_duplicate(env, shared_session):
    order = await _create_order(shared_session)
    name = f"user_{USER}_{order.id}"
    await VPNProfileRepository(shared_session).create(
        user_id=USER, marzban_username=name, sub_url="https://sub.test/stale", status="active"
    )

    assert await _admin(shared_session, env).approve_order(order.id) is True

    profiles = await _profiles(env, name)
    assert len(profiles) == 1
    assert profiles[0].sub_url == env.marzban.users[name]["subscription_url"]
    assert (await _order(env, order.id)).status == "completed"


async def test_foreign_profile_with_same_name_is_not_reused(env, shared_session):
    order = await _create_order(shared_session)
    name = f"user_{USER}_{order.id}"
    await UserRepository(shared_session).get_or_create(OTHER, "other")
    await VPNProfileRepository(shared_session).create(
        user_id=OTHER, marzban_username=name, sub_url="https://sub.test/foreign", status="active"
    )
    await env.marzban.create_user(name, 7 * GB, expire=7)
    env.marzban.calls.clear()
    bot = FakeBot()

    assert await _admin(shared_session, env, bot).approve_order(order.id) is False

    assert env.marzban.count("update_user") == 0
    assert env.marzban.count("create_user") == 0
    assert env.marzban.users[name]["data_limit"] == 7 * GB
    profiles = await _profiles(env, name)
    assert [p.user_id for p in profiles] == [OTHER]
    assert profiles[0].sub_url == "https://sub.test/foreign"
    assert (await _order(env, order.id)).status == "pending"
    assert bot.messages == []


async def test_conflict_with_name_of_another_telegram_id_is_refused(env, shared_session):
    # Имя в Marzban не соответствует схеме user_<telegram_id>_<order_id> этого пользователя
    order = await _create_order(shared_session)
    bad = _admin(shared_session, env)
    assert bad._is_own_marzban_username(f"user_{USER}_{order.id}", USER) is True
    assert bad._is_own_marzban_username(f"user_{OTHER}_{order.id}", USER) is False
    assert bad._is_own_marzban_username(f"user_{USER}_trial", USER) is False
    assert bad._is_own_marzban_username(f"admin_{USER}_{order.id}", USER) is False


async def test_topup_plan_is_dropped_when_user_vanishes_and_order_falls_back_to_new(env, shared_session):
    # Продление с переносом остатка: пользователь исчезает из Marzban между расчётом плана и update
    await UserRepository(shared_session).get_or_create(USER, "tester")
    await VPNProfileRepository(shared_session).create(
        user_id=USER, marzban_username="user_111_5", sub_url="https://sub.test/old", status="active"
    )
    await env.marzban.create_user("user_111_5", 100 * GB)
    env.marzban.set_used("user_111_5", 90 * GB)
    order = await _create_order(shared_session, tariff_gb=50)

    original_update = env.marzban.update_user

    async def vanishing_update(username, **kwargs):
        if username == "user_111_5":
            del env.marzban.users[username]
            from tests.fakes import http_error
            raise http_error(404)
        return await original_update(username, **kwargs)

    env.marzban.update_user = vanishing_update

    assert await _admin(shared_session, env).approve_order(order.id) is True

    new_name = f"user_{USER}_{order.id}"
    assert env.marzban.users[new_name]["data_limit"] == 50 * GB  # без остатка прежнего пакета
    saved = await _order(env, order.id)
    assert saved.carried_over_bytes == 0 and saved.planned_data_limit == 50 * GB
