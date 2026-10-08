from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.domain.models import BalanceEntry, Order, OrderPayment


class BalanceRepository:
    """Чтение бонусного баланса. Баланс = сумма записей append-only журнала.
    Записи создаются только в транзакциях `OrderSettlement`."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_balance(self, user_id: int) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.coalesce(func.sum(BalanceEntry.amount_rub), 0)).where(BalanceEntry.user_id == user_id)
            )
            return int(result.scalar_one())

    async def list_reserves(self, user_id: int) -> list[tuple[int, int]]:
        """(id заказа, удержанная сумма) по ожидающим заказам пользователя."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(OrderPayment.order_id, OrderPayment.balance_rub)
                .join(Order, Order.id == OrderPayment.order_id)
                .where(Order.user_id == user_id, Order.status == "pending", OrderPayment.balance_rub > 0)
                .order_by(OrderPayment.order_id)
            )
            return [(row.order_id, row.balance_rub) for row in result]

    async def recent_entries(self, user_id: int, limit: int = 10) -> list[BalanceEntry]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(BalanceEntry)
                .where(BalanceEntry.user_id == user_id)
                .order_by(BalanceEntry.id.desc())
                .limit(limit)
            )
            return list(result.scalars().all())
