import re
import secrets
from dataclasses import dataclass
from datetime import datetime

BYTES_PER_GB = 1024 ** 3
START_PREFIX = "ref_"
TOKEN_BYTES = 16

_START_PARAM = re.compile(r"ref_([A-Za-z0-9_-]{16,60})")


@dataclass(frozen=True)
class ReferralConfig:
    enabled: bool = True
    invitee_bonus_gb: int = 15
    reward_percent: int = 30
    max_active_links: int = 3
    monthly_cap: int = 10

    @property
    def invitee_bonus_bytes(self) -> int:
        return self.invitee_bonus_gb * BYTES_PER_GB


@dataclass(frozen=True)
class PaymentSplit:
    price_rub: int
    balance_rub: int  # удерживается с бонусного баланса
    cash_rub: int  # остаётся оплатить деньгами


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def build_start_param(token: str) -> str:
    return f"{START_PREFIX}{token}"


def parse_start_param(arg: str | None) -> str | None:
    """Токен из start-параметра `ref_<токен>`; всё остальное - None."""
    if not arg:
        return None
    match = _START_PARAM.fullmatch(arg)
    return match.group(1) if match else None


def build_referral_url(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username.lstrip('@')}?start={build_start_param(token)}"


def calculate_reward(cash_rub: int, percent: int) -> int:
    """Награда пригласившему: процент от денежной части, вниз до целых рублей."""
    if cash_rub <= 0 or percent <= 0:
        return 0
    return cash_rub * percent // 100


def split_payment(price_rub: int, balance_rub: int, balance_limit_rub: int | None = None) -> PaymentSplit:
    """Разбиение цены на удержание с баланса и деньги; `balance_limit_rub` - сумма, показанная на экране."""
    price = max(price_rub, 0)
    held = max(0, min(price, balance_rub))
    if balance_limit_rub is not None:
        held = min(held, max(balance_limit_rub, 0))
    return PaymentSplit(price_rub=price, balance_rub=held, cash_rub=price - held)


def month_bounds(moment: datetime) -> tuple[datetime, datetime]:
    """[начало, начало следующего) календарного месяца по UTC (время в проекте - naive UTC)."""
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        return start, start.replace(year=start.year + 1, month=1)
    return start, start.replace(month=start.month + 1)
