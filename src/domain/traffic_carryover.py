from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

RENEWAL_WINDOW_DAYS = 30

# Marzban статусы, при которых текущая подписка считается уже недействующей
# (сгоревшей) и перенос остатка не производится.
_INACTIVE_STATUSES = {"expired", "limited"}


@dataclass(frozen=True)
class RenewalPlan:
    carried_over_bytes: int
    new_data_limit: int | None  # None = не менять текущий лимит (безлимитный клиент)
    new_expire_at: int  # unix-таймстамп (секунды)
    is_unlimited: bool


def calculate_renewal_plan(
    current_status: str,
    current_data_limit: int | None,
    current_used_traffic: int,
    purchased_bytes: int,
    purchase_time: datetime,
) -> RenewalPlan:
    """Считает перенос остатка трафика при досрочной покупке нового пакета.

    Ничего не знает о сети/Marzban — принимает уже прочитанные значения
    и возвращает готовый план (что записать обратно в панель).
    """
    new_expire_at = int((purchase_time + timedelta(days=RENEWAL_WINDOW_DAYS)).timestamp())

    is_unlimited = current_data_limit is None or current_data_limit == 0
    if is_unlimited:
        return RenewalPlan(
            carried_over_bytes=0,
            new_data_limit=None,
            new_expire_at=new_expire_at,
            is_unlimited=True,
        )

    if current_status in _INACTIVE_STATUSES:
        remaining = 0
    else:
        remaining = max(0, current_data_limit - current_used_traffic)

    return RenewalPlan(
        carried_over_bytes=remaining,
        new_data_limit=purchased_bytes + remaining,
        new_expire_at=new_expire_at,
        is_unlimited=False,
    )


def purchase_time_now() -> datetime:
    return datetime.now(timezone.utc)
