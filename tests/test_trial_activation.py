import asyncio
from datetime import timedelta

import pytest

from src.adapters.db.repositories import VPNProfileRepository
from src.domain.clock import to_unix
from src.domain.models import TrialStatus
from src.domain.trial_rules import TrialAlreadyExistsError
from src.use_cases.trial_use_cases import TrialOfferUseCase, TrialResultKind
from tests.fakes import GB, build_env, make_config


async def test_new_user_gets_trial(env, session_factory):
    result = await env.grant(111)

    assert result.kind == TrialResultKind.GRANTED
    assert result.sub_url == "https://sub.test/user_111_trial"
    assert result.data_gb == 10

    trial = await env.repo.get(111)
    assert trial.status == TrialStatus.ACTIVE
    assert trial.granted_at == env.clock.now
    assert trial.expires_at == env.clock.now + timedelta(days=7)
    assert trial.data_limit_bytes == 10 * GB

    marzban_user = env.marzban.users["user_111_trial"]
    assert marzban_user["data_limit"] == 10 * GB
    assert marzban_user["expire"] == to_unix(env.clock.now + timedelta(days=7))
    assert marzban_user["status"] == "active"

    async with session_factory() as session:
        profile = await VPNProfileRepository(session).get_by_user_id(111)
    assert profile is not None
    assert profile.marzban_username == "user_111_trial"
    assert profile.sub_url == "https://sub.test/user_111_trial"


async def test_repeated_press_does_not_issue_second_trial(env):
    first = await env.grant(111)
    second = await env.grant(111)
    third = await env.grant(111)

    assert first.kind == TrialResultKind.GRANTED
    assert second.kind == third.kind == TrialResultKind.ALREADY_ACTIVE
    assert second.sub_url == first.sub_url
    assert env.marzban.count("create_user") == 1
    assert len(env.marzban.users) == 1


async def test_ended_or_converted_trial_is_never_reissued(env):
    await env.grant(111)
    await env.repo.mark_ended((await env.repo.get(111)).id, "time", env.clock.now)
    assert (await env.grant(111)).kind == TrialResultKind.ALREADY_USED

    await env.repo.mark_converted(111, env.clock.now)
    assert (await env.grant(111)).kind == TrialResultKind.ALREADY_USED
    assert env.marzban.count("create_user") == 1


async def test_trial_survives_deletion_of_marzban_user(env):
    await env.grant(111)
    del env.marzban.users["user_111_trial"]

    # Флаг в нашей БД не зависит от пользователя в Marzban: повторный триал невозможен
    result = await env.grant(111)
    assert result.kind == TrialResultKind.ALREADY_ACTIVE
    assert env.marzban.count("create_user") == 1


async def test_user_with_paid_order_does_not_get_trial(env):
    await env.add_order(111, "completed")

    result = await env.grant(111)

    assert result.kind == TrialResultKind.NOT_ELIGIBLE
    assert await env.repo.get(111) is None
    assert env.marzban.calls == []


async def test_user_with_pending_order_does_not_get_trial(env):
    await env.add_order(111, "pending")

    assert (await env.grant(111)).kind == TrialResultKind.NOT_ELIGIBLE
    assert await env.repo.get(111) is None


async def test_user_with_only_rejected_order_gets_trial(env):
    await env.add_order(111, "rejected")

    assert (await env.grant(111)).kind == TrialResultKind.GRANTED


async def test_offer_follows_eligibility(env):
    offer = TrialOfferUseCase(env.repo, make_config())

    assert await offer.is_offered(111) is True

    await env.grant(111)
    assert await offer.is_offered(111) is False  # триал уже выдан

    await env.add_order(222, "completed")
    assert await offer.is_offered(222) is False  # есть платный заказ

    await env.add_order(333, "pending")
    assert await offer.is_offered(333) is False

    assert await TrialOfferUseCase(env.repo, make_config(enabled=False)).is_offered(444) is False


async def test_trial_disabled_blocks_everything(session_factory):
    env = build_env(session_factory, enabled=False)

    result = await env.grant(111)

    assert result.kind == TrialResultKind.DISABLED
    assert await env.repo.get(111) is None
    assert env.marzban.calls == []


async def test_daily_cap_blocks_new_trials_and_resets_after_24h(session_factory):
    env = build_env(session_factory, daily_cap=2)

    assert (await env.grant(1)).kind == TrialResultKind.GRANTED
    assert (await env.grant(2)).kind == TrialResultKind.GRANTED

    blocked = await env.grant(3)
    assert blocked.kind == TrialResultKind.CAP_REACHED
    assert await env.repo.get(3) is None
    assert env.marzban.count("create_user") == 2

    env.clock.advance(hours=24, seconds=1)
    assert (await env.grant(3)).kind == TrialResultKind.GRANTED


async def test_zero_daily_cap_means_no_new_trials(session_factory):
    env = build_env(session_factory, daily_cap=0)

    assert (await env.grant(1)).kind == TrialResultKind.CAP_REACHED


async def test_rolled_back_reservation_does_not_count_towards_cap(session_factory):
    env = build_env(session_factory, daily_cap=1)
    env.marzban.fail["create_user"] = 1

    assert (await env.grant(1)).kind == TrialResultKind.ERROR
    assert (await env.grant(2)).kind == TrialResultKind.GRANTED


async def test_marzban_failure_rolls_back_reservation_and_retry_has_no_duplicate(env):
    env.marzban.fail["create_user"] = 1

    first = await env.grant(111)

    assert first.kind == TrialResultKind.ERROR
    assert await env.repo.get(111) is None  # флаг не остался
    assert env.marzban.users == {}

    second = await env.grant(111)

    assert second.kind == TrialResultKind.GRANTED
    assert len(env.marzban.users) == 1
    assert (await env.repo.get(111)).status == TrialStatus.ACTIVE


async def test_marzban_unexpected_lookup_error_rolls_back(env):
    env.marzban.fail["get_user"] = 1

    assert (await env.grant(111)).kind == TrialResultKind.ERROR
    assert await env.repo.get(111) is None
    assert (await env.grant(111)).kind == TrialResultKind.GRANTED


async def test_db_failure_after_marzban_creation_is_retried_without_duplicate(env):
    original_activate = env.repo.activate
    calls = {"n": 0}

    async def flaky_activate(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated DB failure")
        return await original_activate(**kwargs)

    env.repo.activate = flaky_activate

    assert (await env.grant(111)).kind == TrialResultKind.ERROR
    assert await env.repo.get(111) is None
    assert len(env.marzban.users) == 1  # пользователь в Marzban уже создан

    result = await env.grant(111)

    assert result.kind == TrialResultKind.GRANTED
    assert env.marzban.count("create_user") == 1  # переиспользован, не создан второй раз
    assert len(env.marzban.users) == 1


async def test_existing_marzban_user_is_reused(env):
    await env.marzban.create_user("user_111_trial", 10 * GB, expire=to_unix(env.clock.now + timedelta(days=3)))
    created_before = env.marzban.count("create_user")

    result = await env.grant(111)

    assert result.kind == TrialResultKind.GRANTED
    assert env.marzban.count("create_user") == created_before
    assert result.sub_url == "https://sub.test/user_111_trial"
    trial = await env.repo.get(111)
    assert trial.expires_at == env.clock.now + timedelta(days=3)  # срок берётся из существующего пользователя


@pytest.mark.parametrize("marzban_user_exists", [False, True])
async def test_stale_reservation_is_resumed(env, marzban_user_exists):
    await env.repo.reserve(111, "tester", "user_111_trial", 10 * GB, env.clock.now)
    if marzban_user_exists:
        await env.marzban.create_user("user_111_trial", 10 * GB)
    created_before = env.marzban.count("create_user")

    result = await env.grant(111)

    assert result.kind == TrialResultKind.GRANTED
    expected_creates = created_before if marzban_user_exists else created_before + 1
    assert env.marzban.count("create_user") == expected_creates
    assert (await env.repo.get(111)).status == TrialStatus.ACTIVE


async def test_parallel_double_press_creates_exactly_one_trial(session_factory):
    env = build_env(session_factory)
    env.marzban.delay = 0.05

    results = await asyncio.gather(env.grant(111), env.grant(111), env.grant(111))

    kinds = sorted(r.kind for r in results)
    assert kinds.count(TrialResultKind.GRANTED) == 1
    assert kinds.count(TrialResultKind.ALREADY_ACTIVE) == 2
    assert env.marzban.count("create_user") == 1
    assert len(env.marzban.users) == 1


async def test_database_uniqueness_protects_against_two_instances_racing(session_factory):
    # Два независимых use case (разные in-process lock-и) - как два процесса бота
    env = build_env(session_factory)
    env.marzban.delay = 0.05
    other = env.new_activate()

    results = await asyncio.gather(env.activate.execute(111, "a"), other.execute(111, "a"))

    assert sum(r.kind == TrialResultKind.GRANTED for r in results) == 1
    assert env.marzban.count("create_user") == 1
    assert len(env.marzban.users) == 1
    assert (await env.repo.get(111)).status == TrialStatus.ACTIVE


async def test_reserve_twice_violates_unique_constraint(env):
    await env.repo.reserve(111, "tester", "user_111_trial", 10 * GB, env.clock.now)

    with pytest.raises(TrialAlreadyExistsError):
        await env.repo.reserve(111, "tester", "user_111_trial", 10 * GB, env.clock.now)
