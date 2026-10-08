import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from src.adapters.db.balance_repository import BalanceRepository
from src.adapters.db.referral_repository import ReferralRepository
from src.domain.clock import utc_now
from src.domain.models import BalanceEntry, ReferralLink, ReferralStatus
from src.domain.referral_rules import (
    ReferralConfig,
    build_referral_url,
    generate_token,
    parse_start_param,
    split_payment,
)
from src.domain.tariffs import get_tariff
from src.domain.trial_rules import SendOutcome
from src.use_cases.order_use_cases import UnknownTariffError

# В реферальном коде исключения БД логируются только по типу: текст исключения SQLAlchemy
# содержит параметры запроса, а там может быть токен ссылки.


class UsernameProvider(Protocol):
    async def get(self) -> str | None: ...


class BotUsernameProvider:
    """Имя бота: `BOT_USERNAME` из конфига или `get_me` (с кэшем). При расхождении пишет warning (один раз)."""

    def __init__(self, bot: Any, configured: str = "") -> None:
        self._bot = bot
        self._configured = configured.strip().lstrip("@")
        self._cached: str | None = None

    async def get(self) -> str | None:
        if self._cached is not None:
            return self._cached

        actual: str | None = None
        try:
            actual = (await self._bot.get_me()).username
        except Exception as exc:
            logger.warning(f"Could not fetch the bot username via get_me ({type(exc).__name__})")

        if self._configured:
            if actual and actual.lower() != self._configured.lower():
                logger.warning("BOT_USERNAME differs from the username reported by Telegram (get_me); the configured value is used")
            self._cached = self._configured
        elif actual:
            self._cached = actual
        return self._cached


class OverviewStatus(StrEnum):
    OK = "ok"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"  # имя бота неизвестно: ссылку собрать нельзя
    NOT_ELIGIBLE = "not_eligible"  # нет подтверждённого заказа с оплатой деньгами: приглашать нельзя


@dataclass(frozen=True)
class LinkView:
    id: int
    url: str


@dataclass(frozen=True)
class ReferralOverview:
    status: OverviewStatus
    links: tuple[LinkView, ...] = ()
    invited: int = 0
    balance: int = 0
    can_create: bool = False
    new_link: ReferralLink | None = None


class ReferralLinkUseCase:
    def __init__(
        self,
        referral_repo: ReferralRepository,
        balance_repo: BalanceRepository,
        username_provider: UsernameProvider,
        config: ReferralConfig,
        clock: Callable[[], datetime] = utc_now,
        token_factory: Callable[[], str] = generate_token,
    ) -> None:
        self.referral_repo = referral_repo
        self.balance_repo = balance_repo
        self.username_provider = username_provider
        self.config = config
        self._clock = clock
        self._token_factory = token_factory
        self._user_locks: dict[int, asyncio.Lock] = {}

    def _lock(self, user_id: int) -> asyncio.Lock:
        lock = self._user_locks.get(user_id)
        if lock is None:
            lock = self._user_locks[user_id] = asyncio.Lock()
        return lock

    async def show(self, user_id: int, username: str | None) -> ReferralOverview:
        """Показывает активные ссылки; если их нет - создаёт одну."""
        if not self.config.enabled:
            return ReferralOverview(OverviewStatus.DISABLED)
        async with self._lock(user_id):
            if not await self.referral_repo.has_cash_paid_order(user_id):
                return ReferralOverview(OverviewStatus.NOT_ELIGIBLE)
            created: ReferralLink | None = None
            if not await self.referral_repo.list_active_links(user_id):
                created = await self._create(user_id, username)
            return await self._overview(user_id, created)

    async def create(self, user_id: int, username: str | None) -> ReferralOverview:
        """Выпускает новую ссылку, если слот свободен; иначе показывает существующие."""
        if not self.config.enabled:
            return ReferralOverview(OverviewStatus.DISABLED)
        async with self._lock(user_id):
            if not await self.referral_repo.has_cash_paid_order(user_id):
                return ReferralOverview(OverviewStatus.NOT_ELIGIBLE)
            created = await self._create(user_id, username)
            return await self._overview(user_id, created)

    async def revoke(self, user_id: int, link_id: int) -> ReferralOverview:
        async with self._lock(user_id):
            if not await self.referral_repo.has_cash_paid_order(user_id):
                return ReferralOverview(OverviewStatus.NOT_ELIGIBLE)
            if await self.referral_repo.revoke_link(user_id, link_id, self._clock()):
                logger.info(f"Referral link {link_id} revoked by user {user_id}")
            return await self._overview(user_id, None)

    async def _create(self, user_id: int, username: str | None) -> ReferralLink | None:
        if await self.username_provider.get() is None:
            return None
        try:
            link = await self.referral_repo.create_link(
                user_id, username, self._token_factory(), self._clock(), self.config.max_active_links
            )
        except SQLAlchemyError as exc:
            logger.error(f"Failed to create a referral link for user {user_id} ({type(exc).__name__})")
            return None
        if link is not None:
            logger.info(f"Referral link {link.id} created by user {user_id}")
        return link

    async def _overview(self, user_id: int, created: ReferralLink | None) -> ReferralOverview:
        bot_username = await self.username_provider.get()
        if bot_username is None:
            logger.warning("Referral links are unavailable: bot username is unknown (set BOT_USERNAME)")
            return ReferralOverview(OverviewStatus.UNAVAILABLE)
        links = await self.referral_repo.list_active_links(user_id)
        return ReferralOverview(
            status=OverviewStatus.OK,
            links=tuple(LinkView(link.id, build_referral_url(bot_username, link.token)) for link in links),
            invited=await self.referral_repo.count_invited(user_id),
            balance=await self.balance_repo.get_balance(user_id),
            can_create=len(links) < self.config.max_active_links,
            new_link=created,
        )


@dataclass(frozen=True)
class BindResult:
    bound: bool


class AcceptReferralUseCase:
    """Привязка приглашённого при открытии ссылки. Причину отказа наружу не раскрывает."""

    def __init__(
        self,
        referral_repo: ReferralRepository,
        config: ReferralConfig,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.referral_repo = referral_repo
        self.config = config
        self._clock = clock

    async def execute(self, invitee_id: int, username: str | None, start_arg: str | None) -> BindResult:
        if not self.config.enabled:
            return BindResult(False)
        token = parse_start_param(start_arg)
        if token is None:
            logger.info(f"User {invitee_id} opened an invalid referral parameter")
            return BindResult(False)
        try:
            outcome = await self.referral_repo.bind(token, invitee_id, username, self._clock())
        except SQLAlchemyError as exc:
            logger.error(f"Referral binding failed for user {invitee_id} ({type(exc).__name__})")
            return BindResult(False)
        if outcome is None:
            logger.info(f"User {invitee_id} opened a referral link that could not be applied")
            return BindResult(False)
        logger.info(
            f"User {invitee_id} bound to inviter {outcome.inviter_id} "
            f"(link {outcome.link_id}, referral {outcome.referral_id})"
        )
        return BindResult(True)


@dataclass(frozen=True)
class PaymentQuote:
    tariff_gb: int
    price_rub: int
    balance_rub: int  # спишется с баланса
    cash_rub: int  # останется оплатить
    balance_total: int
    bonus_gb: int  # бонус приглашённого к этому пакету (0 - не положен)

    @property
    def covers_fully(self) -> bool:
        return self.price_rub > 0 and self.cash_rub == 0


class PaymentQuoteUseCase:
    """Что показать на экране оплаты: сколько спишется с баланса и сколько останется оплатить."""

    def __init__(self, balance_repo: BalanceRepository, referral_repo: ReferralRepository, config: ReferralConfig) -> None:
        self.balance_repo = balance_repo
        self.referral_repo = referral_repo
        self.config = config

    async def quote(self, user_id: int, tariff_gb: int) -> PaymentQuote:
        tariff = get_tariff(tariff_gb)
        if tariff is None or tariff.price_rub <= 0:
            raise UnknownTariffError(str(tariff_gb))
        balance = await self.balance_repo.get_balance(user_id)
        split = split_payment(tariff.price_rub, balance)
        bonus_gb = 0
        if self.config.enabled and split.cash_rub > 0:
            referral = await self.referral_repo.get_by_invitee(user_id)
            if referral is not None and referral.status == ReferralStatus.BOUND and referral.bonus_order_id is None:
                bonus_gb = self.config.invitee_bonus_gb
        return PaymentQuote(
            tariff_gb=tariff_gb,
            price_rub=tariff.price_rub,
            balance_rub=split.balance_rub,
            cash_rub=split.cash_rub,
            balance_total=balance,
            bonus_gb=bonus_gb,
        )


class ReferralNotifier(Protocol):
    async def send_reward(self, telegram_id: int, reward_rub: int, balance_rub: int) -> SendOutcome: ...


class RewardNotificationUseCase:
    """Уведомление пригласившему о начислении. Отправка идёт вне транзакции начисления;
    флаг в БД захватывается атомарно (как у уведомлений триала), блокировка бота помечается и не повторяется."""

    def __init__(
        self,
        referral_repo: ReferralRepository,
        balance_repo: BalanceRepository,
        notifier: ReferralNotifier,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.referral_repo = referral_repo
        self.balance_repo = balance_repo
        self.notifier = notifier
        self._clock = clock

    async def notify(self, referral_id: int) -> None:
        try:
            await self._notify(referral_id)
        except Exception as exc:
            logger.error(f"Reward notification for referral {referral_id} failed ({type(exc).__name__})")

    async def _notify(self, referral_id: int) -> None:
        now = self._clock()
        if not await self.referral_repo.claim_reward_notification(referral_id, now):
            return
        referral = await self.referral_repo.get(referral_id)
        try:
            balance = await self.balance_repo.get_balance(referral.inviter_id)
            outcome = await self.notifier.send_reward(referral.inviter_id, referral.reward_rub or 0, balance)
        except Exception:
            await self.referral_repo.release_reward_notification(referral_id)
            raise
        if outcome == SendOutcome.SENT:
            logger.info(f"Reward notification sent to user {referral.inviter_id} (referral {referral_id})")
        elif outcome == SendOutcome.BLOCKED:
            await self.referral_repo.mark_notify_blocked(referral_id, now)
            logger.warning(f"User {referral.inviter_id} blocked the bot; reward notification will not be retried")
        else:
            await self.referral_repo.release_reward_notification(referral_id)
            logger.warning(f"Reward notification to user {referral.inviter_id} failed, will retry later")

    async def run_once(self) -> None:
        for referral in await self.referral_repo.list_unnotified_rewards():
            await self.notify(referral.id)


@dataclass(frozen=True)
class BalanceOverview:
    balance: int
    reserves: tuple[tuple[int, int], ...]  # (id заказа, удержано)
    entries: tuple[BalanceEntry, ...]


class BalanceOverviewUseCase:
    """Просмотр баланса пользователя администратором (только чтение)."""

    def __init__(self, balance_repo: BalanceRepository) -> None:
        self.balance_repo = balance_repo

    async def overview(self, user_id: int, limit: int = 10) -> BalanceOverview:
        return BalanceOverview(
            balance=await self.balance_repo.get_balance(user_id),
            reserves=tuple(await self.balance_repo.list_reserves(user_id)),
            entries=tuple(await self.balance_repo.recent_entries(user_id, limit)),
        )
