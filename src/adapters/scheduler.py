import asyncio
from collections.abc import Awaitable, Callable
from loguru import logger


async def run_periodically(
    name: str,
    interval_seconds: float,
    job: Callable[[], Awaitable[None]],
) -> None:
    """Запускает job сразу и затем каждые interval_seconds. Ошибки job логируются и не останавливают цикл."""
    logger.info(f"Background job '{name}' started (every {interval_seconds}s)")
    while True:
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"Background job '{name}' failed")
        await asyncio.sleep(interval_seconds)
