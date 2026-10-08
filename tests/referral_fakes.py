import itertools
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.adapters.db.balance_repository import BalanceRepository
from src.adapters.db.order_settlement import OrderSettlement
from src.adapters.db.referral_repository import ReferralRepository
from src.adapters.db.repositories import OrderRepository, UserRepository, VPNProfileRepository
from src.domain.models import BalanceEntry, BalanceKind, Order, OrderPayment, Referral, ReferralLink
from src.domain.referral_rules import ReferralConfig
from src.domain.trial_rules import SendOutcome
from src.use_cases.admin_use_cases import AdminUseCases
from src.use_cases.order_use_cases import CreateOrderUseCase, PlacedOrder
from src.use_cases.referral_use_cases import (
    AcceptReferralUseCase,
    BalanceOverviewUseCase,
    PaymentQuoteUseCase,
    ReferralLinkUseCase,
    RewardNotificationUseCase,
)
from tests.fakes import FakeBot, FakeClock, FakeMarzban, TrialEnv

BOT_NAME = "groove_test_bot"
_fake_ref_ids = itertools.count(900_000)


class CaptureLogs:
    """Перехват сообщений loguru (все уровни)."""

    def __enter__(self) -> "CaptureLogs":
        self.records: list[str] = []
        self._id = logger.add(lambda message: self.records.append(str(message)), level="DEBUG", format="{level} {message}")
        return self

    def __exit__(self, *exc: object) -> None:
        logger.remove(self._id)

    @property
    def text(self) -> str:
        return "\n".join(self.records)


class FakeUsernameProvider:
    def __init__(self, username: str | None = BOT_NAME) -> None:
        self.username = username

    async def get(self) -> str | None:
        return self.username


class FakeReferralNotifier:
    """Запоминает уведомления пригласившим; исходы можно задать очередью."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, int, int]] = []  # (telegram_id, reward, balance)
        self.attempts = 0
        self.outcomes: list[SendOutcome] = []

    async def send_reward(self, telegram_id: int, reward_rub: int, balance_rub: int) -> SendOutcome:
        self.attempts += 1
        outcome = self.outcomes.pop(0) if self.outcomes else SendOutcome.SENT
        if outcome == SendOutcome.SENT:
            self.sent.append((telegram_id, reward_rub, balance_rub))
        return outcome


@dataclass
class RefEnv:
    base: TrialEnv
    shared: AsyncSession
    config: ReferralConfig
    referrals: ReferralRepository
    balance: BalanceRepository
    settlement: OrderSettlement
    username: FakeUsernameProvider
    notifier: FakeReferralNotifier
    reward_notifier: RewardNotificationUseCase
    links: ReferralLinkUseCase
    accept: AcceptReferralUseCase
    quote: PaymentQuoteUseCase
    balance_overview: BalanceOverviewUseCase
    bot: FakeBot = field(default_factory=FakeBot)

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self.base.session_factory

    @property
    def marzban(self) -> FakeMarzban:
        return self.base.marzban

    @property
    def clock(self) -> FakeClock:
        return self.base.clock

    def admin(self, bot: FakeBot | None = None, **overrides: Any) -> AdminUseCases:
        kwargs: dict[str, Any] = dict(
            trial_repo=self.base.repo,
            payments=self.settlement,
            referral_config=self.config,
            reward_notifier=self.reward_notifier,
            clock=self.clock,
        )
        kwargs.update(overrides)
        return AdminUseCases(
            OrderRepository(self.shared),
            UserRepository(self.shared),
            VPNProfileRepository(self.shared),
            self.marzban,
            bot or self.bot,
            **kwargs,
        )

    def orders(self) -> CreateOrderUseCase:
        return CreateOrderUseCase(
            OrderRepository(self.shared),
            UserRepository(self.shared),
            VPNProfileRepository(self.shared),
            settlement=self.settlement,
            clock=self.clock,
        )

    # ---------- помощники для тестов ----------

    async def make_link(self, inviter: int) -> ReferralLink:
        overview = await self.links.create(inviter, f"user{inviter}")
        assert overview.new_link is not None
        return overview.new_link

    async def bind(self, inviter: int, invitee: int) -> str:
        """Выпускает ссылку пригласившего и привязывает приглашённого; возвращает токен."""
        link = await self.make_link(inviter)
        assert (await self.accept.execute(invitee, f"user{invitee}", f"ref_{link.token}")).bound is True
        return link.token

    async def add_balance(self, user_id: int, amount: int, ref_id: int | None = None) -> None:
        async with self.session_factory() as session:
            await UserRepository(session).get_or_create(user_id=user_id, username=None)
            session.add(
                BalanceEntry(
                    user_id=user_id,
                    kind=BalanceKind.REFERRAL_REWARD,
                    amount_rub=amount,
                    ref_id=ref_id if ref_id is not None else next(_fake_ref_ids),
                    created_at=self.clock(),
                )
            )
            await session.commit()

    async def place(
        self, user_id: int, tariff_gb: int = 50, balance_rub: int = 0, photo: str = "photo"
    ) -> PlacedOrder:
        return await self.orders().place(user_id, f"user{user_id}", tariff_gb, photo, balance_rub=balance_rub)

    async def order(self, order_id: int) -> Order:
        async with self.session_factory() as session:
            return await session.get(Order, order_id)

    async def payment(self, order_id: int) -> OrderPayment | None:
        async with self.session_factory() as session:
            return await session.get(OrderPayment, order_id)

    async def referral_of(self, invitee: int) -> Referral | None:
        async with self.session_factory() as session:
            return (await session.execute(select(Referral).where(Referral.invitee_id == invitee))).scalars().first()

    async def entries(self, user_id: int | None = None, kind: str | None = None) -> list[BalanceEntry]:
        async with self.session_factory() as session:
            query = select(BalanceEntry).order_by(BalanceEntry.id)
            if user_id is not None:
                query = query.where(BalanceEntry.user_id == user_id)
            if kind is not None:
                query = query.where(BalanceEntry.kind == kind)
            return list((await session.execute(query)).scalars())

    async def ledger_sum(self, user_id: int) -> int:
        async with self.session_factory() as session:
            return (
                await session.execute(
                    select(func.coalesce(func.sum(BalanceEntry.amount_rub), 0)).where(BalanceEntry.user_id == user_id)
                )
            ).scalar_one()


def build_ref_env(base: TrialEnv, shared: AsyncSession, config: ReferralConfig | None = None) -> RefEnv:
    config = config or ReferralConfig()
    factory = base.session_factory
    referrals = ReferralRepository(factory)
    balance = BalanceRepository(factory)
    settlement = OrderSettlement(factory)
    username = FakeUsernameProvider()
    notifier = FakeReferralNotifier()
    reward_notifier = RewardNotificationUseCase(referrals, balance, notifier, clock=base.clock)
    return RefEnv(
        base=base,
        shared=shared,
        config=config,
        referrals=referrals,
        balance=balance,
        settlement=settlement,
        username=username,
        notifier=notifier,
        reward_notifier=reward_notifier,
        links=ReferralLinkUseCase(referrals, balance, username, config, clock=base.clock),
        accept=AcceptReferralUseCase(referrals, config, clock=base.clock),
        quote=PaymentQuoteUseCase(balance, referrals, config),
        balance_overview=BalanceOverviewUseCase(balance),
    )
