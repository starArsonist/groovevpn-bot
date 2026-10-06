import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.domain.models  # noqa: F401  (регистрирует все модели в metadata)
from src.domain.base import Base
from tests.fakes import TrialEnv, build_env


@pytest.fixture
async def session_factory(tmp_path):
    """Временная файловая SQLite: у каждой сессии своё соединение, поэтому
    гонки и уникальные ограничения ведут себя как на настоящей БД."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'trial_test.sqlite3').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def env(session_factory) -> TrialEnv:
    return build_env(session_factory)
