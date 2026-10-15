import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

import httpx
from loguru import logger

from src.adapters.db.trial_repository import TrialRepository
from src.adapters.marzban.client import MarzbanClient
from src.adapters.marzban.inbounds import build_inbounds_payload
from src.domain.clock import from_unix, to_unix, utc_now
from src.domain.models import Trial, TrialStatus
from src.domain.trial_rules import (
    BYTES_PER_GB,
    DAILY_CAP_WINDOW_HOURS,
    TrialAlreadyExistsError,
    TrialConfig,
)


class TrialResultKind(StrEnum):
    GRANTED = "granted"
    ALREADY_ACTIVE = "already_active"
    ALREADY_USED = "already_used"
    NOT_ELIGIBLE = "not_eligible"
    CAP_REACHED = "cap_reached"
    DISABLED = "disabled"
    ERROR = "error"


@dataclass(frozen=True)
class TrialActivationResult:
    kind: TrialResultKind
    sub_url: str | None = None
    data_gb: float | None = None
    expires_at: datetime | None = None


def trial_marzban_username(telegram_id: int) -> str:
    return f"user_{telegram_id}_trial"


class ActivateTrialUseCase:
    def __init__(
        self,
        trial_repo: TrialRepository,
        marzban_client: MarzbanClient,
        config: TrialConfig,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.trial_repo = trial_repo
        self.marzban_client = marzban_client
        self.config = config
        self._clock = clock
        # Бот работает в одном процессе: lock по пользователю схлопывает двойное
        # нажатие, общий lock делает атомарной связку "проверка лимита + резерв".
        # Окончательная защита от дублей - UNIQUE(telegram_id) в БД.
        self._user_locks: dict[int, asyncio.Lock] = {}
        self._cap_lock = asyncio.Lock()

    def _user_lock(self, telegram_id: int) -> asyncio.Lock:
        lock = self._user_locks.get(telegram_id)
        if lock is None:
            lock = asyncio.Lock()
            self._user_locks[telegram_id] = lock
        return lock

    async def execute(self, telegram_id: int, username: str | None) -> TrialActivationResult:
        if not self.config.enabled:
            logger.info(f"Trial requested by {telegram_id}, but trials are disabled")
            return TrialActivationResult(TrialResultKind.DISABLED)

        async with self._user_lock(telegram_id):
            return await self._execute_locked(telegram_id, username)

    async def _execute_locked(self, telegram_id: int, username: str | None) -> TrialActivationResult:
        trial = await self.trial_repo.get(telegram_id)

        if trial is not None:
            if trial.status != TrialStatus.RESERVED:
                return await self._result_for_existing(trial)
            logger.warning(f"Resuming stale trial reservation for {telegram_id}")
        else:
            if await self.trial_repo.has_paid_order(telegram_id) or await self.trial_repo.has_pending_order(telegram_id):
                logger.info(f"Trial denied for {telegram_id}: has paid or pending order")
                return TrialActivationResult(TrialResultKind.NOT_ELIGIBLE)

            async with self._cap_lock:
                now = self._clock()
                since = now - timedelta(hours=DAILY_CAP_WINDOW_HOURS)
                issued = await self.trial_repo.count_created_since(since)
                if self.config.daily_cap <= 0 or issued >= self.config.daily_cap:
                    logger.warning(f"Trial daily cap reached ({issued}/{self.config.daily_cap}), denied for {telegram_id}")
                    return TrialActivationResult(TrialResultKind.CAP_REACHED)
                try:
                    trial = await self.trial_repo.reserve(
                        telegram_id=telegram_id,
                        username=username,
                        marzban_username=trial_marzban_username(telegram_id),
                        data_limit_bytes=self.config.data_limit_bytes,
                        now=now,
                    )
                except TrialAlreadyExistsError:
                    logger.info(f"Concurrent trial reservation for {telegram_id} lost the race")
                    existing = await self.trial_repo.get(telegram_id)
                    if existing is None:
                        return TrialActivationResult(TrialResultKind.ERROR)
                    return await self._result_for_existing(existing)

        return await self._provision(trial)

    async def _result_for_existing(self, trial: Trial) -> TrialActivationResult:
        if trial.status == TrialStatus.ACTIVE:
            sub_url = await self.trial_repo.get_sub_url(trial.marzban_username)
            return TrialActivationResult(
                TrialResultKind.ALREADY_ACTIVE,
                sub_url=sub_url,
                data_gb=trial.data_limit_bytes / BYTES_PER_GB,
                expires_at=trial.expires_at,
            )
        if trial.status in (TrialStatus.ENDED, TrialStatus.CONVERTED):
            return TrialActivationResult(TrialResultKind.ALREADY_USED)
        # reserved: активацию ведёт другой экземпляр, не дублируем создание пользователя
        logger.warning(f"Trial for {trial.telegram_id} is being activated by another request")
        return TrialActivationResult(TrialResultKind.ERROR)

    async def _provision(self, trial: Trial) -> TrialActivationResult:
        telegram_id = trial.telegram_id
        try:
            granted_at = self._clock()
            planned_expires_at = granted_at + timedelta(days=self.config.days)
            marzban_user = await self._get_or_create_marzban_user(trial, planned_expires_at)

            sub_url = marzban_user.get("subscription_url", "")
            marzban_expire = marzban_user.get("expire")
            expires_at = from_unix(marzban_expire) if marzban_expire else planned_expires_at

            await self.trial_repo.activate(
                telegram_id=telegram_id,
                marzban_username=trial.marzban_username,
                sub_url=sub_url,
                granted_at=granted_at,
                expires_at=expires_at,
            )
        except Exception as exc:
            logger.error(f"Trial activation failed for {telegram_id}: {exc}")
            await self._rollback_reservation(telegram_id)
            return TrialActivationResult(TrialResultKind.ERROR)

        logger.info(f"Trial granted to {telegram_id} ({trial.marzban_username}), expires {expires_at}")
        return TrialActivationResult(
            TrialResultKind.GRANTED,
            sub_url=sub_url,
            data_gb=trial.data_limit_bytes / BYTES_PER_GB,
            expires_at=expires_at,
        )

    async def _get_or_create_marzban_user(self, trial: Trial, expires_at: datetime) -> dict:
        try:
            existing = await self.marzban_client.get_user(trial.marzban_username)
            logger.info(f"Reusing existing Marzban user {trial.marzban_username} for trial")
            return existing
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise

        inbounds = build_inbounds_payload(await self.marzban_client.get_inbounds())
        return await self.marzban_client.create_user(
            username=trial.marzban_username,
            data_limit=trial.data_limit_bytes,
            expire=to_unix(expires_at),
            inbounds=inbounds,
        )

    async def _rollback_reservation(self, telegram_id: int) -> None:
        try:
            await self.trial_repo.release(telegram_id)
            logger.info(f"Trial reservation for {telegram_id} rolled back")
        except Exception as exc:
            # Резерв останется в статусе reserved и будет подхвачен следующим нажатием
            logger.error(f"Failed to roll back trial reservation for {telegram_id}: {exc}")


class TrialOfferUseCase:
    """Решает, показывать ли кнопку триала на стартовом экране."""

    def __init__(self, trial_repo: TrialRepository, config: TrialConfig) -> None:
        self.trial_repo = trial_repo
        self.config = config

    async def is_offered(self, telegram_id: int) -> bool:
        if not self.config.enabled:
            return False
        if await self.trial_repo.get(telegram_id) is not None:
            return False
        if await self.trial_repo.has_paid_order(telegram_id):
            return False
        return not await self.trial_repo.has_pending_order(telegram_id)
