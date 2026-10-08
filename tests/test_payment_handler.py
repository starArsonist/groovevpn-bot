from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from telegram.ext import ConversationHandler

from src.adapters.db.repositories import OrderRepository, UserRepository, VPNProfileRepository
from src.adapters.tg_bot.handlers.payment import receipt_photo_handler
from src.domain.models import Order
from src.use_cases.order_use_cases import CreateOrderUseCase

ADMIN = 999
USER = 111


@pytest.fixture
async def shared_session(session_factory):
    async with session_factory() as session:
        yield session


def _context(shared_session, user_data: dict):
    uc = CreateOrderUseCase(
        OrderRepository(shared_session), UserRepository(shared_session), VPNProfileRepository(shared_session)
    )
    bot = SimpleNamespace(send_photo=AsyncMock())
    return SimpleNamespace(
        user_data=user_data,
        bot=bot,
        bot_data={"create_order_uc": uc, "config": SimpleNamespace(telegram_admin_id=ADMIN)},
    )


def _update():
    message = SimpleNamespace(photo=[SimpleNamespace(file_id="receipt")], reply_text=AsyncMock())
    return SimpleNamespace(effective_user=SimpleNamespace(id=USER, username="t"), message=message), message.reply_text


async def _orders_count(session_factory) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(Order))).scalar_one()


@pytest.mark.parametrize("user_data", [{}, {"selected_tariff": 0}, {"selected_tariff": 77}])
async def test_receipt_without_known_tariff_creates_no_order(session_factory, shared_session, user_data):
    context = _context(shared_session, user_data)
    update, reply = _update()

    result = await receipt_photo_handler(update, context)

    assert result == ConversationHandler.END
    assert await _orders_count(session_factory) == 0
    context.bot.send_photo.assert_not_awaited()
    reply.assert_awaited_once()


async def test_receipt_with_known_tariff_still_creates_order(session_factory, shared_session):
    context = _context(shared_session, {"selected_tariff": 50})
    update, _ = _update()

    await receipt_photo_handler(update, context)

    assert await _orders_count(session_factory) == 1
    context.bot.send_photo.assert_awaited_once()
