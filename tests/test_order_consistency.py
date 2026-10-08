import pytest
from sqlalchemy import update

from src.adapters.db.repositories import OrderRepository, UserRepository, VPNProfileRepository
from src.domain.models import Order
from src.use_cases.admin_use_cases import AdminUseCases
from src.use_cases.order_use_cases import CreateOrderUseCase
from tests.fakes import FakeBot

USER = 111


@pytest.fixture
async def shared_session(session_factory):
    # Как в main.py: у AdminUseCases одна общая сессия
    async with session_factory() as session:
        yield session


def _admin(shared_session, env, bot=None) -> AdminUseCases:
    return AdminUseCases(
        OrderRepository(shared_session),
        UserRepository(shared_session),
        VPNProfileRepository(shared_session),
        env.marzban,
        bot or FakeBot(),
        trial_repo=env.repo,
    )


async def _create_order(shared_session, tariff_gb: int = 50):
    uc = CreateOrderUseCase(
        OrderRepository(shared_session), UserRepository(shared_session), VPNProfileRepository(shared_session)
    )
    return await uc.execute(USER, "tester", tariff_gb, "photo")


async def _set_status_from_other_session(env, order_id: int, status: str) -> None:
    async with env.session_factory() as other:
        await other.execute(update(Order).where(Order.id == order_id).values(status=status))
        await other.commit()


# ---------- A1: статус читается мимо кэша общей сессии ----------

async def test_approve_ignores_stale_cached_status(env, shared_session):
    order = await _create_order(shared_session)
    await OrderRepository(shared_session).get_by_id(order.id)  # объект с status=pending закэширован
    await _set_status_from_other_session(env, order.id, "completed")

    assert await _admin(shared_session, env).approve_order(order.id) is False

    assert env.marzban.count("create_user") == 0


async def test_reject_ignores_stale_cached_status(env, shared_session):
    order = await _create_order(shared_session)
    await OrderRepository(shared_session).get_by_id(order.id)
    await _set_status_from_other_session(env, order.id, "completed")
    bot = FakeBot()

    assert await _admin(shared_session, env, bot).reject_order(order.id) is False

    assert bot.messages == []
    async with env.session_factory() as check:
        assert (await check.get(Order, order.id)).status == "completed"
