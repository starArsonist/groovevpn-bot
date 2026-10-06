import time

import pytest

from src.adapters.db.repositories import OrderRepository, UserRepository, VPNProfileRepository
from src.domain.models import TrialStatus
from src.use_cases.admin_use_cases import AdminUseCases
from src.use_cases.order_use_cases import CreateOrderUseCase
from tests.fakes import GB, FakeBot

USER = 111
NAME = "user_111_trial"


@pytest.fixture
async def shared_session(session_factory):
    # Как в main.py: у AdminUseCases одна общая сессия
    async with session_factory() as session:
        yield session


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


def _assert_expires_in_30_days(unix_expire: int) -> None:
    assert abs(unix_expire - (time.time() + 30 * 24 * 3600)) < 120


async def test_trial_user_purchase_updates_existing_user_without_carryover(env, shared_session):
    await env.grant(USER)
    env.marzban.set_used(NAME, 3 * GB)  # остаток триала 7 ГБ
    order = await _create_order(shared_session)
    assert order.order_type == "topup"  # триал-пользователь идёт по ветке обновления
    bot = FakeBot()

    assert await _admin(shared_session, env, bot).approve_order(order.id) is True

    marzban_user = env.marzban.users[NAME]
    assert marzban_user["data_limit"] == 50 * GB  # ровно пакет, без остатка триала
    assert marzban_user["used_traffic"] == 0
    assert marzban_user["status"] == "active"
    _assert_expires_in_30_days(marzban_user["expire"])
    assert len(env.marzban.users) == 1
    assert env.marzban.count("create_user") == 1  # только создание триала

    trial = await env.repo.get(USER)
    assert trial.status == TrialStatus.CONVERTED
    assert trial.converted_at is not None

    stored = await OrderRepository(shared_session).get_by_id(order.id)
    assert stored.status == "completed"
    assert stored.carried_over_bytes == 0
    assert len(bot.messages) == 1
    assert "Перенесено" not in bot.messages[0][1]
    assert "50" in bot.messages[0][1]


@pytest.mark.parametrize("final_status,used", [("expired", 1 * GB), ("limited", 10 * GB)])
async def test_ended_trial_user_purchase_restores_access(env, shared_session, final_status, used):
    await env.grant(USER)
    env.marzban.set_used(NAME, used)
    env.marzban.set_status(NAME, final_status)
    order = await _create_order(shared_session)

    assert await _admin(shared_session, env).approve_order(order.id) is True

    marzban_user = env.marzban.users[NAME]
    assert marzban_user["status"] == "active"
    assert marzban_user["data_limit"] == 50 * GB
    assert marzban_user["used_traffic"] == 0
    assert env.marzban.count("create_user") == 1
    assert (await env.repo.get(USER)).status == TrialStatus.CONVERTED


async def test_repeated_approve_of_same_order_changes_nothing(env, shared_session):
    await env.grant(USER)
    env.marzban.set_used(NAME, 3 * GB)
    order = await _create_order(shared_session)
    bot = FakeBot()
    admin = _admin(shared_session, env, bot)
    assert await admin.approve_order(order.id) is True
    converted_at = (await env.repo.get(USER)).converted_at
    calls_after_first = list(env.marzban.calls)
    snapshot = dict(env.marzban.users[NAME])

    assert await admin.approve_order(order.id) is False
    assert await _admin(shared_session, env, bot).approve_order(order.id) is False  # другой экземпляр use case

    assert env.marzban.calls == calls_after_first
    assert env.marzban.users[NAME] == snapshot
    assert (await env.repo.get(USER)).converted_at == converted_at
    assert len(bot.messages) == 1


async def test_crash_after_reset_then_retry_converts_without_double_effects(env, shared_session):
    await env.grant(USER)
    env.marzban.set_used(NAME, 3 * GB)
    order = await _create_order(shared_session)
    env.marzban.fail["update_user"] = 1
    admin = _admin(shared_session, env)
    get_user_before = env.marzban.count("get_user")

    assert await admin.approve_order(order.id) is False

    # сбой между reset и update: триал ещё не конвертирован, заказ pending
    assert env.marzban.users[NAME]["used_traffic"] == 0
    assert (await env.repo.get(USER)).status == TrialStatus.ACTIVE
    assert (await OrderRepository(shared_session).get_by_id(order.id)).status == "pending"

    assert await admin.approve_order(order.id) is True

    assert env.marzban.users[NAME]["data_limit"] == 50 * GB
    assert env.marzban.count("reset_user_data_usage") == 1
    assert env.marzban.count("get_user") == get_user_before + 1  # план не пересчитывался
    assert (await env.repo.get(USER)).status == TrialStatus.CONVERTED


async def test_two_orders_before_first_approval_second_is_regular_paid_carryover(env, shared_session):
    await env.grant(USER)
    env.marzban.set_used(NAME, 3 * GB)
    first = await _create_order(shared_session)
    second = await _create_order(shared_session)  # оба созданы, пока триал ещё не конвертирован
    admin = _admin(shared_session, env)

    assert await admin.approve_order(first.id) is True
    assert env.marzban.users[NAME]["data_limit"] == 50 * GB

    env.marzban.set_used(NAME, 20 * GB)  # клиент потратил 20 ГБ платного пакета
    assert await admin.approve_order(second.id) is True

    # платный перенос как прежде: остаток 30 ГБ + новый пакет 50 ГБ
    assert env.marzban.users[NAME]["data_limit"] == 80 * GB
    assert env.marzban.users[NAME]["used_traffic"] == 0
    stored = await OrderRepository(shared_session).get_by_id(second.id)
    assert stored.carried_over_bytes == 30 * GB


async def test_trial_user_deleted_in_marzban_falls_back_to_new_user_and_converts(env, shared_session):
    await env.grant(USER)
    del env.marzban.users[NAME]
    order = await _create_order(shared_session)

    assert await _admin(shared_session, env).approve_order(order.id) is True

    new_name = f"user_{USER}_{order.id}"
    assert env.marzban.users[new_name]["data_limit"] == 50 * GB
    assert (await env.repo.get(USER)).status == TrialStatus.CONVERTED
    profile = await VPNProfileRepository(shared_session).get_by_user_id(USER)
    assert profile.marzban_username == new_name


async def test_regular_paid_user_still_gets_carryover_when_trial_feature_is_wired(env, shared_session):
    # Платный клиент без триала: перенос остатка не меняется
    await UserRepository(shared_session).get_or_create(USER, "tester")
    await VPNProfileRepository(shared_session).create(
        user_id=USER, marzban_username="user_111_5", sub_url="https://sub.test/old", status="active"
    )
    await env.marzban.create_user("user_111_5", 100 * GB)
    env.marzban.set_used("user_111_5", 90 * GB)
    order = await _create_order(shared_session, tariff_gb=100)

    assert await _admin(shared_session, env).approve_order(order.id) is True

    assert env.marzban.users["user_111_5"]["data_limit"] == 110 * GB
    assert await env.repo.get(USER) is None


async def test_first_paid_purchase_without_trial_creates_user_as_before(env, shared_session):
    order = await _create_order(shared_session)
    assert order.order_type == "new"

    assert await _admin(shared_session, env).approve_order(order.id) is True

    assert f"user_{USER}_{order.id}" in env.marzban.users
    assert await env.repo.get(USER) is None
