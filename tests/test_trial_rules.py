from datetime import datetime, timedelta

from src.domain.trial_rules import (
    ENDED_BY_TIME,
    ENDED_BY_TRAFFIC,
    build_trial_conversion_plan,
    evaluate_trial,
)

GB = 1024 ** 3
NOW = datetime(2026, 10, 6, 12, 0, 0)
LIMIT = 10 * GB


def _evaluate(used, status="active", expires_in_days=7):
    return evaluate_trial(
        limit_bytes=LIMIT,
        used_bytes=used,
        marzban_status=status,
        expires_at=NOW + timedelta(days=expires_in_days),
        now=NOW,
    )


def test_below_threshold_nothing_happens():
    snapshot = _evaluate(7.9 * GB // 1)
    assert not snapshot.reached_low
    assert not snapshot.ended


def test_eighty_percent_reaches_low_but_not_ended():
    snapshot = _evaluate(8 * GB)
    assert snapshot.reached_low
    assert not snapshot.ended
    assert snapshot.used_ratio == 0.8


def test_traffic_exhausted_ends_trial():
    snapshot = _evaluate(10 * GB)
    assert snapshot.ended
    assert snapshot.ended_reason == ENDED_BY_TRAFFIC
    assert snapshot.reached_low


def test_marzban_limited_status_ends_trial_by_traffic():
    snapshot = _evaluate(None, status="limited")
    assert snapshot.ended
    assert snapshot.ended_reason == ENDED_BY_TRAFFIC


def test_expiry_time_ends_trial_by_time():
    snapshot = evaluate_trial(LIMIT, 1 * GB, "active", NOW - timedelta(seconds=1), NOW)
    assert snapshot.ended
    assert snapshot.ended_reason == ENDED_BY_TIME
    assert not snapshot.reached_low


def test_marzban_expired_status_ends_trial_by_time():
    snapshot = _evaluate(1 * GB, status="expired")
    assert snapshot.ended
    assert snapshot.ended_reason == ENDED_BY_TIME


def test_traffic_wins_when_both_conditions_hold():
    snapshot = evaluate_trial(LIMIT, 10 * GB, "active", NOW - timedelta(days=1), NOW)
    assert snapshot.ended_reason == ENDED_BY_TRAFFIC


def test_missing_marzban_data_still_checks_time():
    active = evaluate_trial(LIMIT, None, None, NOW + timedelta(days=1), NOW)
    assert not active.ended and not active.reached_low

    expired = evaluate_trial(LIMIT, None, None, NOW - timedelta(days=1), NOW)
    assert expired.ended
    assert expired.ended_reason == ENDED_BY_TIME


def test_conversion_plan_never_carries_trial_remainder():
    purchase_time = datetime(2026, 10, 6, 12, 0, 0)
    plan = build_trial_conversion_plan(50 * GB, purchase_time)

    assert plan.carried_over_bytes == 0
    assert plan.new_data_limit == 50 * GB
    assert plan.is_unlimited is False
    assert plan.new_expire_at == int((purchase_time + timedelta(days=30)).timestamp())
