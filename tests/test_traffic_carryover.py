from datetime import datetime, timedelta, timezone

from src.domain.traffic_carryover import calculate_renewal_plan

GB = 1024 ** 3


def test_example_from_spec_carries_over_remainder():
    # 100 ГБ лимит, потрачено 90 ГБ, докупают 100 ГБ -> перенос 10 ГБ, итого 110 ГБ
    purchase_time = datetime(2026, 9, 7, tzinfo=timezone.utc)

    plan = calculate_renewal_plan(
        current_status="active",
        current_data_limit=100 * GB,
        current_used_traffic=90 * GB,
        purchased_bytes=100 * GB,
        purchase_time=purchase_time,
    )

    assert plan.carried_over_bytes == 10 * GB
    assert plan.new_data_limit == 110 * GB
    assert plan.is_unlimited is False

    expected_expire = purchase_time + timedelta(days=30)
    assert plan.new_expire_at == int(expected_expire.timestamp())


def test_expired_subscription_has_no_carryover():
    purchase_time = datetime(2026, 9, 7, tzinfo=timezone.utc)

    plan = calculate_renewal_plan(
        current_status="expired",
        current_data_limit=100 * GB,
        current_used_traffic=10 * GB,  # неважно, подписка уже истекла
        purchased_bytes=50 * GB,
        purchase_time=purchase_time,
    )

    assert plan.carried_over_bytes == 0
    assert plan.new_data_limit == 50 * GB
    # даже без переноса, expire всё равно выставляется на новые 30 дней
    assert plan.new_expire_at == int((purchase_time + timedelta(days=30)).timestamp())


def test_limited_status_has_no_carryover():
    plan = calculate_renewal_plan(
        current_status="limited",
        current_data_limit=100 * GB,
        current_used_traffic=100 * GB,
        purchased_bytes=50 * GB,
        purchase_time=datetime(2026, 9, 7, tzinfo=timezone.utc),
    )

    assert plan.carried_over_bytes == 0
    assert plan.new_data_limit == 50 * GB


def test_unlimited_user_has_nothing_to_carry_and_limit_is_untouched():
    purchase_time = datetime(2026, 9, 7, tzinfo=timezone.utc)

    for unlimited_value in (None, 0):
        plan = calculate_renewal_plan(
            current_status="active",
            current_data_limit=unlimited_value,
            current_used_traffic=123,
            purchased_bytes=50 * GB,
            purchase_time=purchase_time,
        )

        assert plan.is_unlimited is True
        assert plan.carried_over_bytes == 0
        assert plan.new_data_limit is None  # лимит не меняем
        # expire всё равно продлевается
        assert plan.new_expire_at == int((purchase_time + timedelta(days=30)).timestamp())


def test_used_traffic_greater_than_limit_clamps_to_zero():
    # Ситуация "used > limit" не должна давать отрицательный перенос
    plan = calculate_renewal_plan(
        current_status="active",
        current_data_limit=50 * GB,
        current_used_traffic=70 * GB,
        purchased_bytes=50 * GB,
        purchase_time=datetime(2026, 9, 7, tzinfo=timezone.utc),
    )

    assert plan.carried_over_bytes == 0
    assert plan.new_data_limit == 50 * GB


def test_two_consecutive_early_purchases_stack_remainders():
    # Первая досрочная покупка: 100/90 -> +100 = 110 ГБ, remaining 10 ГБ
    first_purchase_time = datetime(2026, 9, 7, tzinfo=timezone.utc)
    first_plan = calculate_renewal_plan(
        current_status="active",
        current_data_limit=100 * GB,
        current_used_traffic=90 * GB,
        purchased_bytes=100 * GB,
        purchase_time=first_purchase_time,
    )
    assert first_plan.new_data_limit == 110 * GB

    # После применения первого плана в Marzban: used_traffic сброшен в 0,
    # data_limit = 110 ГБ. Клиент почти сразу докупает ещё 100 ГБ, успев
    # потратить 20 ГБ.
    second_purchase_time = first_purchase_time + timedelta(days=1)
    second_plan = calculate_renewal_plan(
        current_status="active",
        current_data_limit=first_plan.new_data_limit,
        current_used_traffic=20 * GB,
        purchased_bytes=100 * GB,
        purchase_time=second_purchase_time,
    )

    # remaining = 110 - 20 = 90, итог = 100 + 90 = 190 ГБ
    assert second_plan.carried_over_bytes == 90 * GB
    assert second_plan.new_data_limit == 190 * GB
    # срок каждый раз считается заново от момента последней покупки
    assert second_plan.new_expire_at == int((second_purchase_time + timedelta(days=30)).timestamp())
    assert second_plan.new_expire_at != first_plan.new_expire_at
