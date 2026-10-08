import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

T = TypeVar("T")

LOCK_RETRIES = 3
LOCK_BACKOFF_SECONDS = 0.05


async def run_in_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    work: Callable[[AsyncSession], Awaitable[T]],
    retries: int = LOCK_RETRIES,
) -> T:
    """Выполняет `work` в одной транзакции на отдельной сессии (commit при успехе, rollback при любой ошибке).

    SQLite при гонке записей может ответить "database is locked", когда ожидание
    блокировки истекло: такая транзакция безопасно повторяется целиком.
    """
    attempt = 0
    while True:
        async with session_factory() as session:
            try:
                result = await work(session)
                await session.commit()
                return result
            except OperationalError as exc:
                await session.rollback()
                if "locked" not in str(exc.orig).lower() or attempt >= retries:
                    raise
            except BaseException:
                await session.rollback()
                raise
        attempt += 1
        await asyncio.sleep(LOCK_BACKOFF_SECONDS * attempt)
