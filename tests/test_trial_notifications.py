from src.domain.models import TrialStatus
from src.domain.trial_rules import SendOutcome, TrialNotificationKind as Kind
from tests.fakes import GB, FakeNotifier

USER = 1
NAME = "user_1_trial"


async def _ticks(env, count: int = 3) -> None:
    for _ in range(count):
        await env.monitor.run_once()


async def test_below_eighty_percent_sends_nothing(env):
    await env.grant(USER)
    env.marzban.set_used(NAME, int(7.9 * GB))

    await _ticks(env)

    assert env.notifier.sent == []


async def test_eighty_percent_sends_one_notification_and_no_duplicates(env):
    await env.grant(USER)
    env.marzban.set_used(NAME, 8 * GB)

    await _ticks(env, 4)

    assert env.notifier.sent == [(USER, Kind.LOW)]
    trial = await env.repo.get(USER)
    assert trial.status == TrialStatus.ACTIVE
    assert trial.reached_80_at == env.clock.now
    assert trial.notified_low_at is not None


async def test_trial_ended_by_traffic_sends_one_notification(env):
    await env.grant(USER)
    env.marzban.set_used(NAME, 10 * GB)

    await _ticks(env, 4)

    # Окно "осталось мало" пропущено - только сообщение о завершении
    assert env.notifier.sent == [(USER, Kind.ENDED)]
    trial = await env.repo.get(USER)
    assert trial.status == TrialStatus.ENDED
    assert trial.ended_reason == "traffic"
    assert trial.ended_at == env.clock.now
    assert trial.reached_80_at is not None


async def test_marzban_limited_status_ends_trial(env):
    await env.grant(USER)
    env.marzban.set_status(NAME, "limited")

    await _ticks(env)

    assert env.notifier.sent == [(USER, Kind.ENDED)]
    assert (await env.repo.get(USER)).ended_reason == "traffic"


async def test_trial_ended_by_time_sends_one_notification(env):
    await env.grant(USER)
    env.marzban.set_used(NAME, 1 * GB)
    env.clock.advance(days=7, minutes=1)

    await _ticks(env, 4)

    assert env.notifier.sent == [(USER, Kind.ENDED)]
    trial = await env.repo.get(USER)
    assert trial.status == TrialStatus.ENDED
    assert trial.ended_reason == "time"


async def test_full_lifecycle_sends_at_most_three_then_silence(env):
    await env.grant(USER)

    env.marzban.set_used(NAME, 8 * GB)
    await _ticks(env)

    env.marzban.set_used(NAME, 10 * GB)
    await _ticks(env)

    env.clock.advance(hours=23, minutes=59)
    await _ticks(env)
    assert env.notifier.kinds_for(USER) == [Kind.LOW, Kind.ENDED]  # напоминание ещё рано

    env.clock.advance(minutes=2)
    await _ticks(env)
    assert env.notifier.kinds_for(USER) == [Kind.LOW, Kind.ENDED, Kind.REMINDER]

    env.clock.advance(days=30)
    await _ticks(env, 5)
    assert len(env.notifier.sent) == 3  # дальше тишина


async def test_restart_does_not_duplicate_notifications(env):
    await env.grant(USER)
    env.marzban.set_used(NAME, 8 * GB)
    await _ticks(env, 2)
    assert env.notifier.sent == [(USER, Kind.LOW)]

    after_restart = FakeNotifier()
    await env.new_monitor(after_restart).run_once()
    assert after_restart.sent == []

    env.marzban.set_used(NAME, 10 * GB)
    await env.new_monitor(after_restart).run_once()
    assert after_restart.sent == [(USER, Kind.ENDED)]

    again = FakeNotifier()
    await env.new_monitor(again).run_once()
    assert again.sent == []

    env.clock.advance(hours=25)
    await env.new_monitor(again).run_once()
    assert again.sent == [(USER, Kind.REMINDER)]

    final = FakeNotifier()
    await env.new_monitor(final).run_once()
    assert final.sent == []


async def test_blocked_user_is_marked_and_never_retried(env):
    await env.grant(USER)
    env.notifier.outcomes[USER] = [SendOutcome.BLOCKED]
    env.marzban.set_used(NAME, 8 * GB)

    await _ticks(env)
    assert env.notifier.attempts == 1
    trial = await env.repo.get(USER)
    assert trial.bot_blocked_at is not None

    env.marzban.set_used(NAME, 10 * GB)
    await _ticks(env)
    env.clock.advance(hours=48)
    await _ticks(env)

    assert env.notifier.attempts == 1  # больше не пытаемся
    # аналитика при этом продолжает собираться
    trial = await env.repo.get(USER)
    assert trial.status == TrialStatus.ENDED
    assert trial.ended_at is not None


async def test_converted_user_gets_no_trial_notifications(env):
    await env.grant(USER)
    env.marzban.set_used(NAME, 8 * GB)
    await env.repo.mark_converted(USER, env.clock.now)

    await _ticks(env)
    env.marzban.set_used(NAME, 10 * GB)
    env.clock.advance(days=10)
    await _ticks(env)

    assert env.notifier.sent == []


async def test_paid_order_without_conversion_mark_is_healed_and_silenced(env):
    await env.grant(USER)
    env.marzban.set_used(NAME, 8 * GB)
    await env.add_order(USER, "completed")  # сбой между подтверждением заказа и меткой триала

    await _ticks(env)

    assert env.notifier.sent == []
    assert (await env.repo.get(USER)).status == TrialStatus.CONVERTED


async def test_transient_send_failure_releases_flag_and_retries_later(env):
    await env.grant(USER)
    env.notifier.outcomes[USER] = [SendOutcome.FAILED]
    env.marzban.set_used(NAME, 8 * GB)

    await env.monitor.run_once()
    assert env.notifier.sent == []
    assert (await env.repo.get(USER)).notified_low_at is None

    await _ticks(env, 3)
    assert env.notifier.sent == [(USER, Kind.LOW)]


async def test_monitor_uses_batch_requests_not_one_per_user(env):
    for user_id in range(1, 31):
        await env.grant(user_id)
        env.marzban.set_used(f"user_{user_id}_trial", 8 * GB)
    single_lookups_before = env.marzban.count("get_user")

    await env.monitor.run_once()

    assert env.marzban.count("get_users") == 1
    assert env.marzban.count("get_user") == single_lookups_before
    assert len(env.notifier.sent) == 30


async def test_marzban_outage_does_not_break_monitor_and_time_is_still_checked(env):
    await env.grant(USER)
    env.marzban.fail["get_users"] = 1
    env.clock.advance(days=8)

    await env.monitor.run_once()  # не должен выбросить исключение

    assert env.notifier.sent == [(USER, Kind.ENDED)]
    assert (await env.repo.get(USER)).ended_reason == "time"
