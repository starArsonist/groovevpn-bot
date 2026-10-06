from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from loguru import logger

from src.adapters.db.trial_repository import TrialRepository
from src.adapters.marzban.client import MarzbanClient
from src.domain.clock import utc_now
from src.domain.models import Trial, TrialStatus
from src.domain.trial_rules import (
    REMINDER_DELAY_HOURS,
    SendOutcome,
    TrialNotificationKind,
    evaluate_trial,
)


class TrialNotifier(Protocol):
    async def send(
        self,
        telegram_id: int,
        kind: TrialNotificationKind,
        *,
        used_bytes: int | None,
        limit_bytes: int,
        ended_reason: str | None,
    ) -> SendOutcome: ...


@dataclass(frozen=True)
class _PendingNotification:
    trial: Trial
    kind: TrialNotificationKind
    used_bytes: int | None
    ended_reason: str | None


class TrialMonitorUseCase:
    """Периодическая пакетная проверка триалов и отправка не более трёх уведомлений."""

    def __init__(
        self,
        trial_repo: TrialRepository,
        marzban_client: MarzbanClient,
        notifier: TrialNotifier,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.trial_repo = trial_repo
        self.marzban_client = marzban_client
        self.notifier = notifier
        self._clock = clock

    async def run_once(self) -> None:
        now = self._clock()
        trials = await self.trial_repo.list_monitored()
        if not trials:
            return

        trials = await self._heal_converted(trials, now)
        usage = await self._fetch_usage([t for t in trials if t.status == TrialStatus.ACTIVE])

        pending: list[_PendingNotification] = []
        reminder_cutoff = now - timedelta(hours=REMINDER_DELAY_HOURS)

        for trial in trials:
            if trial.status == TrialStatus.ACTIVE:
                pending += await self._evaluate_active(trial, usage, now)
            else:
                pending += self._pending_for_ended(trial, reminder_cutoff)

        for item in pending:
            await self._dispatch(item, now, reminder_cutoff)

    async def _heal_converted(self, trials: list[Trial], now: datetime) -> list[Trial]:
        """Пользователи с подтверждённым платным заказом больше не получают триал-уведомлений."""
        paid = await self.trial_repo.paid_user_ids([t.telegram_id for t in trials])
        remaining: list[Trial] = []
        for trial in trials:
            if trial.telegram_id in paid:
                await self.trial_repo.mark_converted(trial.telegram_id, now)
                logger.info(f"Trial of {trial.telegram_id} marked converted by monitor (paid order exists)")
            else:
                remaining.append(trial)
        return remaining

    async def _fetch_usage(self, active: list[Trial]) -> dict[str, dict[str, Any]]:
        if not active:
            return {}
        try:
            users = await self.marzban_client.get_users([t.marzban_username for t in active])
        except Exception as exc:
            logger.error(f"Failed to fetch trial usage from Marzban: {exc}")
            return {}
        return {u["username"]: u for u in users if "username" in u}

    async def _evaluate_active(
        self, trial: Trial, usage: dict[str, dict[str, Any]], now: datetime
    ) -> list[_PendingNotification]:
        marzban_user = usage.get(trial.marzban_username)
        used_bytes = marzban_user.get("used_traffic") if marzban_user else None
        snapshot = evaluate_trial(
            limit_bytes=trial.data_limit_bytes,
            used_bytes=used_bytes,
            marzban_status=marzban_user.get("status") if marzban_user else None,
            expires_at=trial.expires_at,
            now=now,
        )

        if snapshot.reached_low and trial.reached_80_at is None:
            await self.trial_repo.mark_reached_low(trial.id, now)
            logger.info(f"Trial of {trial.telegram_id} reached 80% of traffic")

        if snapshot.ended:
            await self.trial_repo.mark_ended(trial.id, snapshot.ended_reason or "time", now)
            logger.info(f"Trial of {trial.telegram_id} ended ({snapshot.ended_reason})")
            # Окно "осталось мало" пропущено - сразу сообщение о завершении
            return [_PendingNotification(trial, TrialNotificationKind.ENDED, used_bytes, snapshot.ended_reason)]

        if snapshot.reached_low:
            return [_PendingNotification(trial, TrialNotificationKind.LOW, used_bytes, None)]
        return []

    @staticmethod
    def _pending_for_ended(trial: Trial, reminder_cutoff: datetime) -> list[_PendingNotification]:
        items: list[_PendingNotification] = []
        if trial.notified_ended_at is None:
            items.append(_PendingNotification(trial, TrialNotificationKind.ENDED, None, trial.ended_reason))
        if (
            trial.notified_reminder_at is None
            and trial.ended_at is not None
            and trial.ended_at <= reminder_cutoff
        ):
            items.append(_PendingNotification(trial, TrialNotificationKind.REMINDER, None, trial.ended_reason))
        return items

    async def _dispatch(self, item: _PendingNotification, now: datetime, reminder_cutoff: datetime) -> None:
        trial = item.trial
        claimed = await self.trial_repo.claim_notification(trial.id, item.kind, now, reminder_cutoff)
        if not claimed:
            return

        outcome = await self.notifier.send(
            trial.telegram_id,
            item.kind,
            used_bytes=item.used_bytes,
            limit_bytes=trial.data_limit_bytes,
            ended_reason=item.ended_reason,
        )

        if outcome == SendOutcome.SENT:
            logger.info(f"Trial notification '{item.kind}' sent to {trial.telegram_id}")
        elif outcome == SendOutcome.BLOCKED:
            await self.trial_repo.mark_blocked(trial.id, now)
            logger.warning(f"User {trial.telegram_id} blocked the bot; trial notifications stopped")
        else:
            await self.trial_repo.release_notification(trial.id, item.kind)
            logger.warning(f"Trial notification '{item.kind}' to {trial.telegram_id} failed, will retry later")
