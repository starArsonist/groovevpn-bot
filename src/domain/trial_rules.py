from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from src.domain.traffic_carryover import RenewalPlan, RENEWAL_WINDOW_DAYS

BYTES_PER_GB = 1024 ** 3
LOW_TRAFFIC_THRESHOLD = 0.8
REMINDER_DELAY_HOURS = 24
DAILY_CAP_WINDOW_HOURS = 24

ENDED_BY_TRAFFIC = "traffic"
ENDED_BY_TIME = "time"


class TrialAlreadyExistsError(Exception):
    """Запись триала для этого Telegram-аккаунта уже существует (нарушение уникальности)."""


class TrialNotificationKind(StrEnum):
    LOW = "low"
    ENDED = "ended"
    REMINDER = "reminder"


class SendOutcome(StrEnum):
    SENT = "sent"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class TrialConfig:
    enabled: bool
    data_gb: int
    days: int
    daily_cap: int

    @property
    def data_limit_bytes(self) -> int:
        return self.data_gb * BYTES_PER_GB


@dataclass(frozen=True)
class TrialSnapshot:
    used_ratio: float | None
    reached_low: bool
    ended: bool
    ended_reason: str | None


def evaluate_trial(
    limit_bytes: int,
    used_bytes: int | None,
    marzban_status: str | None,
    expires_at: datetime | None,
    now: datetime,
) -> TrialSnapshot:
    """Оценивает состояние триала по данным Marzban и сроку из нашей БД.

    Триал заканчивается по тому, что наступит раньше: трафик исчерпан или срок
    вышел. Если данных Marzban нет (used_bytes/marzban_status = None), трафик не
    оценивается, срок по-прежнему проверяется по нашей БД.
    """
    used_ratio = used_bytes / limit_bytes if used_bytes is not None and limit_bytes > 0 else None

    traffic_ended = marzban_status == "limited" or (used_ratio is not None and used_ratio >= 1.0)
    time_ended = marzban_status == "expired" or (expires_at is not None and now >= expires_at)

    ended_reason: str | None = None
    if traffic_ended:
        ended_reason = ENDED_BY_TRAFFIC
    elif time_ended:
        ended_reason = ENDED_BY_TIME

    reached_low = traffic_ended or (used_ratio is not None and used_ratio >= LOW_TRAFFIC_THRESHOLD)

    return TrialSnapshot(
        used_ratio=used_ratio,
        reached_low=reached_low,
        ended=ended_reason is not None,
        ended_reason=ended_reason,
    )


def build_trial_conversion_plan(purchased_bytes: int, purchase_time: datetime) -> RenewalPlan:
    """План покупки платного пакета триал-пользователем: остаток триала не переносится.

    Триал не платный пакет, поэтому carried_over_bytes всегда 0, новый лимит
    равен размеру купленного пакета, срок - 30 дней от момента покупки.
    """
    new_expire_at = int((purchase_time + timedelta(days=RENEWAL_WINDOW_DAYS)).timestamp())
    return RenewalPlan(
        carried_over_bytes=0,
        new_data_limit=purchased_bytes,
        new_expire_at=new_expire_at,
        is_unlimited=False,
    )
