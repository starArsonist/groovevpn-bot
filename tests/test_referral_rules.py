from datetime import datetime

import pytest

from src.domain.referral_rules import (
    ReferralConfig,
    build_referral_url,
    build_start_param,
    calculate_reward,
    generate_token,
    month_bounds,
    parse_start_param,
    split_payment,
)

TELEGRAM_START_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")


def test_token_is_random_and_fits_start_parameter_limits():
    tokens = {generate_token() for _ in range(200)}
    assert len(tokens) == 200
    for token in tokens:
        param = build_start_param(token)
        assert param.startswith("ref_")
        assert len(param) <= 64
        assert set(param) <= TELEGRAM_START_ALPHABET


def test_parse_start_param_roundtrip_and_garbage():
    token = generate_token()
    assert parse_start_param(build_start_param(token)) == token
    for bad in (None, "", "ref_", "ref_short", "xyz_" + token, "ref_" + token + "!", "ref_" + "a" * 65, "REF_" + token, " ref_" + token):
        assert parse_start_param(bad) is None


def test_referral_url_format_and_username_normalization():
    token = generate_token()
    assert build_referral_url("GrooveVPN_bot", token) == f"https://t.me/GrooveVPN_bot?start=ref_{token}"
    assert build_referral_url("@GrooveVPN_bot", token) == f"https://t.me/GrooveVPN_bot?start=ref_{token}"


@pytest.mark.parametrize("cash,expected", [(130, 39), (250, 75), (449, 134), (0, 0), (3, 0), (4, 1), (91, 27)])
def test_reward_is_floor_of_percent_of_cash(cash, expected):
    assert calculate_reward(cash, 30) == expected


def test_reward_is_zero_for_negative_cash_or_percent():
    assert calculate_reward(-5, 30) == 0
    assert calculate_reward(100, 0) == 0


@pytest.mark.parametrize(
    "price,balance,limit,expected",
    [
        (130, 0, None, (0, 130)),
        (130, 39, None, (39, 91)),
        (130, 500, None, (130, 0)),
        (130, 130, None, (130, 0)),
        (130, 500, 39, (39, 91)),      # показанное на экране ограничивает удержание
        (130, 20, 39, (20, 110)),      # баланс уже уменьшился
        (130, -5, None, (0, 130)),
        (0, 50, None, (0, 0)),
    ],
)
def test_split_payment(price, balance, limit, expected):
    split = split_payment(price, balance, limit)
    assert (split.balance_rub, split.cash_rub) == expected
    assert split.balance_rub + split.cash_rub == max(price, 0)


def test_month_bounds_are_utc_calendar_month():
    start, end = month_bounds(datetime(2026, 10, 31, 23, 59, 59))
    assert (start, end) == (datetime(2026, 10, 1), datetime(2026, 11, 1))
    start, end = month_bounds(datetime(2026, 12, 15, 3))
    assert (start, end) == (datetime(2026, 12, 1), datetime(2027, 1, 1))


def test_config_bonus_bytes_follow_gb_setting():
    assert ReferralConfig(invitee_bonus_gb=15).invitee_bonus_bytes == 15 * 1024 ** 3
    assert ReferralConfig(invitee_bonus_gb=7).invitee_bonus_bytes == 7 * 1024 ** 3
