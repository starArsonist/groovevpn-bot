from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from loguru import logger
from sqlalchemy import DateTime, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.adapters.db.referral_repository import ensure_user
from src.adapters.db.transactions import run_in_transaction
from src.domain.models import (
    BalanceEntry,
    BalanceKind,
    CloseReason,
    Order,
    OrderPayment,
    Referral,
    ReferralStatus,
)
from src.domain.referral_rules import ReferralConfig, calculate_reward, month_bounds, split_payment

BALANCE_PHOTO = "balance"  # метка заказа, оплаченного балансом целиком (вместо file_id чека)
PLACE_ORDER_ATTEMPTS = 4


class InsufficientBalance(Exception):
    """Баланса не хватает для оплаты заказа целиком (или цена не положительна)."""


class _BalanceChanged(Exception):
    """Баланс изменился между чтением и вставкой удержания: транзакцию нужно повторить."""


class CancelOutcome(StrEnum):
    CANCELLED = "cancelled"
    NOT_PENDING = "not_pending"
    NOT_OWNER = "not_owner"
    IN_PROGRESS = "in_progress"  # выдача пакета уже начата, отменять нельзя


@dataclass(frozen=True)
class PlacedPayment:
    price_rub: int
    balance_rub: int
    cash_rub: int


@dataclass(frozen=True)
class RewardResult:
    referral_id: int
    inviter_id: int
    amount_rub: int


@dataclass(frozen=True)
class CompletionResult:
    completed: bool
    reward: RewardResult | None = None
    closed_reason: str | None = None


@dataclass(frozen=True)
class PlacedOrderRecord:
    order: Order
    payment: OrderPayment


class OrderSettlement:
    """Денежные транзакции заказа: создание с удержанием баланса, завершение с наградой,
    отклонение и отмена с возвратом. Каждая операция - одна транзакция БД."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ---------- создание заказа с удержанием ----------

    async def place_order(
        self,
        *,
        user_id: int,
        username: str | None,
        tariff_gb: int,
        order_type: str,
        photo_file_id: str,
        price_rub: int,
        balance_limit_rub: int,
        now: datetime,
        require_full: bool = False,
    ) -> PlacedOrderRecord:
        """Заказ + запись об оплате + удержание баланса в одной транзакции.

        Удержание = min(баланс, показанная на экране сумма, цена). Вставка записи журнала
        условна (баланс >= удержания в момент вставки), поэтому параллельные заказы не
        потратят баланс дважды и не уведут его в минус; при проигранной гонке транзакция повторяется.
        """
        if price_rub <= 0:
            raise ValueError("Order price must be positive")

        async def work(session: AsyncSession) -> PlacedOrderRecord:
            await ensure_user(session, user_id, username)
            balance = (
                await session.execute(
                    select(func.coalesce(func.sum(BalanceEntry.amount_rub), 0)).where(BalanceEntry.user_id == user_id)
                )
            ).scalar_one()
            split = split_payment(price_rub, balance, balance_limit_rub)
            if require_full and split.cash_rub != 0:
                raise InsufficientBalance()

            order = Order(
                user_id=user_id,
                tariff_gb=tariff_gb,
                order_type=order_type,
                status="pending",
                photo_file_id=photo_file_id,
            )
            session.add(order)
            await session.flush()
            payment = OrderPayment(
                order_id=order.id,
                price_rub=split.price_rub,
                balance_rub=split.balance_rub,
                cash_rub=split.cash_rub,
                bonus_bytes=0,
                created_at=now,
            )
            session.add(payment)
            await session.flush()

            if split.balance_rub > 0:
                balance_now = (
                    select(func.coalesce(func.sum(BalanceEntry.amount_rub), 0))
                    .where(BalanceEntry.user_id == user_id)
                    .scalar_subquery()
                )
                result = await session.execute(
                    insert(BalanceEntry).from_select(
                        ["user_id", "kind", "amount_rub", "ref_id", "created_at"],
                        select(
                            literal(user_id),
                            literal(BalanceKind.ORDER_SPEND),
                            literal(-split.balance_rub),
                            literal(order.id),
                            literal(now, DateTime()),
                        ).where(balance_now >= split.balance_rub),
                    )
                )
                if result.rowcount != 1:
                    raise _BalanceChanged()
            return PlacedOrderRecord(order=order, payment=payment)

        for attempt in range(PLACE_ORDER_ATTEMPTS):
            try:
                return await run_in_transaction(self._session_factory, work)
            except _BalanceChanged:
                logger.warning(f"Balance of user {user_id} changed while placing an order (attempt {attempt + 1}); retrying")
        raise RuntimeError(f"Could not place order for user {user_id}: balance kept changing")

    # ---------- чтение / ленивое создание записи об оплате ----------

    async def get_payment(self, order_id: int) -> OrderPayment | None:
        async with self._session_factory() as session:
            return await session.get(OrderPayment, order_id)

    async def ensure_payment(self, order_id: int, price_rub: int, now: datetime) -> OrderPayment:
        """Запись об оплате заказа; для заказов, созданных до фичи, - оплата деньгами целиком по цене тарифа."""

        async def work(session: AsyncSession) -> OrderPayment:
            payment = await session.get(OrderPayment, order_id)
            if payment is None:
                payment = OrderPayment(
                    order_id=order_id,
                    price_rub=price_rub,
                    balance_rub=0,
                    cash_rub=price_rub,
                    bonus_bytes=0,
                    created_at=now,
                )
                session.add(payment)
                await session.flush()
            return payment

        return await run_in_transaction(self._session_factory, work)

    async def has_hold(self, order_id: int, amount_rub: int) -> bool:
        """Есть ли в журнале удержание `amount_rub` под этот заказ."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(BalanceEntry.amount_rub).where(
                    BalanceEntry.kind == BalanceKind.ORDER_SPEND, BalanceEntry.ref_id == order_id
                )
            )
            return result.scalar_one_or_none() == -amount_rub

    # ---------- заявка на бонус приглашённого ----------

    async def claim_invitee_bonus(
        self, order_id: int, invitee_id: int, bonus_bytes: int, now: datetime, enabled: bool
    ) -> int:
        """Закрепляет бонус и награду за этим заказом; возвращает байты бонуса, вошедшие в план.

        Идемпотентно: повтор для того же заказа возвращает ранее сохранённый бонус и ничего не меняет.
        """

        async def work(session: AsyncSession) -> int:
            referral = (
                await session.execute(select(Referral).where(Referral.invitee_id == invitee_id))
            ).scalars().first()
            payment = await session.get(OrderPayment, order_id)
            if referral is None or payment is None:
                return 0
            if referral.bonus_order_id == order_id:
                return payment.bonus_bytes
            if not enabled or referral.status != ReferralStatus.BOUND or payment.cash_rub <= 0:
                return 0
            claimed = await session.execute(
                update(Referral)
                .where(
                    Referral.invitee_id == invitee_id,
                    Referral.status == ReferralStatus.BOUND,
                    Referral.bonus_order_id.is_(None),
                )
                .values(bonus_order_id=order_id)
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                return 0
            await session.execute(
                update(OrderPayment)
                .where(OrderPayment.order_id == order_id)
                .values(bonus_bytes=bonus_bytes)
                .execution_options(synchronize_session=False)
            )
            logger.info(f"Invitee bonus claimed for order {order_id} (referral {referral.id})")
            return bonus_bytes

        return await run_in_transaction(self._session_factory, work)

    # ---------- завершение заказа ----------

    async def complete(self, order_id: int, now: datetime, config: ReferralConfig) -> CompletionResult:
        """pending -> completed и всё, что с этим связано, в одной транзакции.

        Повторный вызов (статус уже не pending) ничего не меняет. Награда пригласившему
        записывается тем же коммитом, что и статус заказа.
        """

        async def work(session: AsyncSession) -> CompletionResult:
            moved = await session.execute(
                update(Order)
                .where(Order.id == order_id, Order.status == "pending")
                .values(status="completed")
                .execution_options(synchronize_session=False)
            )
            if moved.rowcount != 1:
                return CompletionResult(completed=False)

            user_id = (await session.execute(select(Order.user_id).where(Order.id == order_id))).scalar_one()
            payment = await session.get(OrderPayment, order_id)
            referral = (
                await session.execute(
                    select(Referral).where(Referral.invitee_id == user_id, Referral.status == ReferralStatus.BOUND)
                )
            ).scalars().first()
            if referral is None:
                return CompletionResult(completed=True)

            if referral.bonus_order_id is None:
                # Первый подтверждённый заказ без права на бонус: оплата балансом целиком
                # или фича была выключена при расчёте плана. Привязка закрывается лениво, здесь.
                reason = CloseReason.NO_CASH if payment is not None and payment.cash_rub == 0 else CloseReason.NOT_ELIGIBLE
                await self._close(session, referral.id, reason, now)
                logger.info(f"Referral {referral.id} closed without reward ({reason}) by order {order_id}")
                return CompletionResult(completed=True, closed_reason=reason)

            if referral.bonus_order_id != order_id:
                return CompletionResult(completed=True)  # заявка у другого ожидающего заказа

            reward = calculate_reward(payment.cash_rub if payment is not None else 0, config.reward_percent)
            if reward <= 0:
                await self._close(session, referral.id, CloseReason.NO_REWARD, now)
                logger.info(f"Referral {referral.id} closed: reward rounds to zero")
                return CompletionResult(completed=True, closed_reason=CloseReason.NO_REWARD)

            month_start, month_end = month_bounds(now)
            rewarded_this_month = (
                select(func.count())
                .select_from(Referral)
                .where(
                    Referral.inviter_id == referral.inviter_id,
                    Referral.status == ReferralStatus.REWARDED,
                    Referral.rewarded_at >= month_start,
                    Referral.rewarded_at < month_end,
                )
                .scalar_subquery()
            )
            rewarded = await session.execute(
                update(Referral)
                .where(
                    Referral.id == referral.id,
                    Referral.status == ReferralStatus.BOUND,
                    Referral.bonus_order_id == order_id,
                    rewarded_this_month < config.monthly_cap,
                )
                .values(status=ReferralStatus.REWARDED, reward_rub=reward, rewarded_at=now, resolved_at=now)
                .execution_options(synchronize_session=False)
            )
            if rewarded.rowcount != 1:
                await self._close(session, referral.id, CloseReason.MONTHLY_CAP, now)
                logger.info(f"Referral {referral.id} closed: monthly cap of inviter {referral.inviter_id} reached")
                return CompletionResult(completed=True, closed_reason=CloseReason.MONTHLY_CAP)

            session.add(
                BalanceEntry(
                    user_id=referral.inviter_id,
                    kind=BalanceKind.REFERRAL_REWARD,
                    amount_rub=reward,
                    ref_id=referral.id,
                    created_at=now,
                )
            )
            await session.flush()
            logger.info(f"Referral {referral.id}: reward {reward} RUB accrued to user {referral.inviter_id}")
            return CompletionResult(
                completed=True,
                reward=RewardResult(referral_id=referral.id, inviter_id=referral.inviter_id, amount_rub=reward),
            )

        return await run_in_transaction(self._session_factory, work)

    @staticmethod
    async def _close(session: AsyncSession, referral_id: int, reason: str, now: datetime) -> None:
        await session.execute(
            update(Referral)
            .where(Referral.id == referral_id, Referral.status == ReferralStatus.BOUND)
            .values(status=ReferralStatus.CLOSED, close_reason=reason, resolved_at=now)
            .execution_options(synchronize_session=False)
        )

    # ---------- отклонение и отмена ----------

    async def reject(self, order_id: int, now: datetime) -> bool:
        return await run_in_transaction(
            self._session_factory, lambda session: self._release(session, order_id, "rejected", now)
        )

    async def cancel(self, order_id: int, user_id: int, now: datetime) -> CancelOutcome:
        async def work(session: AsyncSession) -> CancelOutcome:
            order = await session.get(Order, order_id)
            if order is None or order.user_id != user_id:
                return CancelOutcome.NOT_OWNER
            if order.status != "pending":
                return CancelOutcome.NOT_PENDING
            if order.plan_computed:
                return CancelOutcome.IN_PROGRESS
            released = await self._release(session, order_id, "cancelled", now, user_id=user_id, require_no_plan=True)
            return CancelOutcome.CANCELLED if released else CancelOutcome.NOT_PENDING

        return await run_in_transaction(self._session_factory, work)

    @staticmethod
    async def _release(
        session: AsyncSession,
        order_id: int,
        new_status: str,
        now: datetime,
        user_id: int | None = None,
        require_no_plan: bool = False,
    ) -> bool:
        """pending -> rejected/cancelled, возврат удержания и освобождение заявки на бонус - одной транзакцией."""
        conditions = [Order.id == order_id, Order.status == "pending"]
        if user_id is not None:
            conditions.append(Order.user_id == user_id)
        if require_no_plan:
            conditions.append(Order.plan_computed.is_(False))
        moved = await session.execute(
            update(Order).where(*conditions).values(status=new_status).execution_options(synchronize_session=False)
        )
        if moved.rowcount != 1:
            return False

        owner_id = (await session.execute(select(Order.user_id).where(Order.id == order_id))).scalar_one()
        payment = await session.get(OrderPayment, order_id)
        if payment is not None and payment.balance_rub > 0:
            session.add(
                BalanceEntry(
                    user_id=owner_id,
                    kind=BalanceKind.ORDER_REFUND,
                    amount_rub=payment.balance_rub,
                    ref_id=order_id,
                    created_at=now,
                )
            )
            await session.flush()
            logger.info(f"Order {order_id} {new_status}: {payment.balance_rub} RUB returned to balance of user {owner_id}")
        await session.execute(
            update(Referral)
            .where(Referral.bonus_order_id == order_id, Referral.status == ReferralStatus.BOUND)
            .values(bonus_order_id=None)
            .execution_options(synchronize_session=False)
        )
        if payment is not None and payment.bonus_bytes:
            await session.execute(
                update(OrderPayment)
                .where(OrderPayment.order_id == order_id)
                .values(bonus_bytes=0)
                .execution_options(synchronize_session=False)
            )
        return True
