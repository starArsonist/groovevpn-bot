import asyncio

import pytest
from sqlalchemy.exc import IntegrityError

from src.domain.models import BalanceEntry, BalanceKind, Order, OrderPayment
from src.adapters.db.repositories import OrderRepository
from src.adapters.db.order_settlement import CancelOutcome, InsufficientBalance
from src.use_cases.order_use_cases import UnknownTariffError
from tests.fakes import GB, FakeBot

USER = 111
OTHER = 222


# ---------- создание заказа и удержание ----------

async def test_order_without_balance_has_no_hold(renv):
    placed = await renv.place(USER, 50)

    payment = await renv.payment(placed.order.id)
    assert (payment.price_rub, payment.balance_rub, payment.cash_rub) == (130, 0, 130)
    assert await renv.entries(USER) == []


async def test_partial_balance_is_held_when_order_is_created(renv):
    await renv.add_balance(USER, 39)

    placed = await renv.place(USER, 50, balance_rub=39)

    payment = await renv.payment(placed.order.id)
    assert (payment.balance_rub, payment.cash_rub) == (39, 91)
    spend = await renv.entries(USER, BalanceKind.ORDER_SPEND)
    assert [(e.amount_rub, e.ref_id) for e in spend] == [(-39, placed.order.id)]
    assert await renv.balance.get_balance(USER) == 0


async def test_hold_is_capped_by_price_and_by_amount_shown_on_screen(renv):
    await renv.add_balance(USER, 500)

    shown = await renv.place(USER, 50, balance_rub=39)
    assert (await renv.payment(shown.order.id)).balance_rub == 39

    big = await renv.place(USER, 50, balance_rub=10_000)
    assert (await renv.payment(big.order.id)).balance_rub == 130  # не больше цены
    assert await renv.balance.get_balance(USER) == 500 - 39 - 130


async def test_hold_is_capped_by_current_balance(renv):
    await renv.add_balance(USER, 20)

    placed = await renv.place(USER, 50, balance_rub=39)  # на экране было 39, а осталось 20

    payment = await renv.payment(placed.order.id)
    assert (payment.balance_rub, payment.cash_rub) == (20, 110)
    assert await renv.balance.get_balance(USER) == 0


async def test_unconfirmed_order_keeps_the_hold_and_balance_cannot_be_spent_twice(renv):
    await renv.add_balance(USER, 130)
    first = await renv.place(USER, 50, balance_rub=130)

    second = await renv.place(USER, 50, balance_rub=130)

    assert (await renv.payment(first.order.id)).balance_rub == 130
    assert (await renv.payment(second.order.id)).balance_rub == 0
    assert await renv.ledger_sum(USER) == 0


async def test_unknown_tariff_creates_no_order(renv):
    with pytest.raises(UnknownTariffError):
        await renv.place(USER, 77)
    with pytest.raises(UnknownTariffError):
        await renv.place(USER, 0)
    assert await renv.entries() == []
    assert await renv.order(1) is None


async def test_full_balance_order_requires_price_and_enough_balance(renv):
    await renv.add_balance(USER, 129)

    with pytest.raises(InsufficientBalance):
        await renv.orders().place(USER, "u", 50, "balance", balance_rub=129, require_full=True)

    assert await renv.order(1) is None
    assert await renv.balance.get_balance(USER) == 129  # ничего не удержано

    await renv.add_balance(USER, 1)
    placed = await renv.orders().place(USER, "u", 50, "balance", balance_rub=130, require_full=True)
    payment = await renv.payment(placed.order.id)
    assert (payment.balance_rub, payment.cash_rub) == (130, 0)
    assert placed.order.photo_file_id == "balance"
    assert await renv.balance.get_balance(USER) == 0


# ---------- возврат: отклонение и отмена ----------

async def test_reject_refunds_the_hold_exactly_once(renv):
    await renv.add_balance(USER, 100)
    placed = await renv.place(USER, 50, balance_rub=100)
    admin = renv.admin()

    assert await admin.reject_order(placed.order.id) is True
    assert await admin.reject_order(placed.order.id) is False

    assert await renv.balance.get_balance(USER) == 100
    refunds = await renv.entries(USER, BalanceKind.ORDER_REFUND)
    assert [(e.amount_rub, e.ref_id) for e in refunds] == [(100, placed.order.id)]
    assert (await renv.order(placed.order.id)).status == "rejected"


async def test_user_can_cancel_own_unpaid_order_and_balance_returns(renv):
    await renv.add_balance(USER, 100)
    placed = await renv.place(USER, 50, balance_rub=100)
    admin = renv.admin()

    assert await admin.cancel_order(placed.order.id, USER) is CancelOutcome.CANCELLED
    assert await admin.cancel_order(placed.order.id, USER) is CancelOutcome.NOT_PENDING

    assert await renv.balance.get_balance(USER) == 100
    assert (await renv.order(placed.order.id)).status == "cancelled"
    assert len(await renv.entries(USER, BalanceKind.ORDER_REFUND)) == 1


async def test_cancel_of_foreign_order_is_refused(renv):
    placed = await renv.place(USER, 50)

    assert await renv.admin().cancel_order(placed.order.id, OTHER) is CancelOutcome.NOT_OWNER
    assert (await renv.order(placed.order.id)).status == "pending"


async def test_cancelled_order_cannot_be_approved_or_rejected(renv):
    placed = await renv.place(USER, 50)
    admin = renv.admin()
    await admin.cancel_order(placed.order.id, USER)

    assert await admin.approve_order(placed.order.id) is False
    assert await admin.reject_order(placed.order.id) is False

    assert renv.marzban.count("create_user") == 0
    assert (await renv.order(placed.order.id)).status == "cancelled"


async def test_approved_order_cannot_be_cancelled_or_rejected_and_nothing_is_refunded(renv):
    await renv.add_balance(USER, 100)
    placed = await renv.place(USER, 50, balance_rub=100)
    admin = renv.admin()
    assert await admin.approve_order(placed.order.id) is True

    assert await admin.cancel_order(placed.order.id, USER) is CancelOutcome.NOT_PENDING
    assert await admin.reject_order(placed.order.id) is False

    assert await renv.entries(USER, BalanceKind.ORDER_REFUND) == []
    assert await renv.balance.get_balance(USER) == 0


async def test_cancel_is_refused_once_issuance_started(renv):
    await renv.add_balance(USER, 100)
    placed = await renv.place(USER, 50, balance_rub=100)
    renv.marzban.fail["create_user"] = 1
    admin = renv.admin()
    assert await admin.approve_order(placed.order.id) is False  # план уже сохранён, Marzban упал

    assert await admin.cancel_order(placed.order.id, USER) is CancelOutcome.IN_PROGRESS

    assert (await renv.order(placed.order.id)).status == "pending"
    assert await renv.balance.get_balance(USER) == 0  # резерв на месте
    assert await admin.approve_order(placed.order.id) is True  # повтор идемпотентен


async def test_approve_cancel_and_reject_race_leaves_exactly_one_outcome(renv):
    await renv.add_balance(USER, 100)
    placed = await renv.place(USER, 50, balance_rub=100)
    renv.marzban.delay = 0.1
    admin = renv.admin()

    approve = asyncio.create_task(admin.approve_order(placed.order.id))
    await asyncio.sleep(0)
    cancel = asyncio.create_task(admin.cancel_order(placed.order.id, USER))
    reject = asyncio.create_task(admin.reject_order(placed.order.id))
    approved, cancelled, rejected = await asyncio.gather(approve, cancel, reject)

    assert approved is True
    assert cancelled is CancelOutcome.NOT_PENDING and rejected is False
    assert (await renv.order(placed.order.id)).status == "completed"
    assert await renv.entries(USER, BalanceKind.ORDER_REFUND) == []
    assert await renv.ledger_sum(USER) == 0


# ---------- параллельность ----------

async def test_parallel_orders_never_overspend_balance(renv):
    await renv.add_balance(USER, 100)
    place = lambda: renv.settlement.place_order(  # noqa: E731
        user_id=USER, username="u", tariff_gb=50, order_type="new", photo_file_id="photo",
        price_rub=130, balance_limit_rub=50, now=renv.clock(),
    )

    placed = await asyncio.gather(*(place() for _ in range(6)))

    holds = [p.payment.balance_rub for p in placed]
    assert sum(holds) == 100
    assert all(0 <= h <= 50 for h in holds)
    assert await renv.ledger_sum(USER) == 0
    spend = await renv.entries(USER, BalanceKind.ORDER_SPEND)
    assert sorted(-e.amount_rub for e in spend) == sorted(h for h in holds if h)
    assert len({e.ref_id for e in spend}) == len(spend)


async def test_parallel_orders_and_refunds_keep_balance_consistent(renv):
    await renv.add_balance(USER, 130)
    first = await renv.place(USER, 50, balance_rub=130)

    async def reject_first():
        return await renv.admin().reject_order(first.order.id)

    place = lambda: renv.settlement.place_order(  # noqa: E731
        user_id=USER, username="u", tariff_gb=50, order_type="new", photo_file_id="photo",
        price_rub=130, balance_limit_rub=130, now=renv.clock(),
    )
    # админ отклоняет первый заказ, пока пользователь создаёт ещё два
    results = await asyncio.gather(place(), reject_first(), place())

    total_hold = sum(p.payment.balance_rub for p in (results[0], results[2]))
    assert results[1] is True
    assert total_hold <= 130
    assert await renv.ledger_sum(USER) == 130 - total_hold >= 0


async def test_ledger_rejects_duplicate_spend_and_refund_for_same_order(renv):
    await renv.add_balance(USER, 100)
    placed = await renv.place(USER, 50, balance_rub=100)

    for kind, amount in ((BalanceKind.ORDER_SPEND, -5), (BalanceKind.ORDER_REFUND, 5)):
        async with renv.session_factory() as session:
            session.add(BalanceEntry(user_id=USER, kind=kind, amount_rub=amount, ref_id=placed.order.id))
            if kind == BalanceKind.ORDER_SPEND:
                with pytest.raises(IntegrityError):
                    await session.commit()
            else:
                await session.commit()  # первый возврат допустим
    async with renv.session_factory() as session:
        session.add(BalanceEntry(user_id=USER, kind=BalanceKind.ORDER_REFUND, amount_rub=5, ref_id=placed.order.id))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_ledger_rejects_zero_amount(renv):
    async with renv.session_factory() as session:
        session.add(BalanceEntry(user_id=USER, kind=BalanceKind.REFERRAL_REWARD, amount_rub=0, ref_id=1))
        with pytest.raises(IntegrityError):
            await session.commit()


# ---------- заказ без реальной цены не подтверждается ----------

async def _raw_order(renv, photo: str, tariff_gb: int = 50) -> int:
    async with renv.session_factory() as session:
        order = Order(user_id=USER, tariff_gb=tariff_gb, order_type="new", status="pending", photo_file_id=photo)
        session.add(order)
        await session.commit()
        return order.id


async def test_order_with_unknown_tariff_is_not_approved(renv):
    order_id = await _raw_order(renv, "photo", tariff_gb=77)

    assert await renv.admin().approve_order(order_id) is False

    assert renv.marzban.count("create_user") == 0
    assert (await renv.order(order_id)).status == "pending"


async def test_balance_order_without_hold_is_not_approved(renv):
    order_id = await _raw_order(renv, "balance")  # метка оплаты балансом, но платежа и удержания нет

    assert await renv.admin().approve_order(order_id) is False

    assert renv.marzban.count("create_user") == 0
    assert (await renv.order(order_id)).status == "pending"


async def test_zero_cash_order_without_balance_marker_is_not_approved(renv):
    order_id = await _raw_order(renv, "photo")
    async with renv.session_factory() as session:
        session.add(OrderPayment(order_id=order_id, price_rub=130, balance_rub=130, cash_rub=0))
        await session.commit()

    assert await renv.admin().approve_order(order_id) is False

    assert renv.marzban.count("create_user") == 0


async def test_balance_order_whose_hold_entry_is_missing_is_not_approved(renv):
    order_id = await _raw_order(renv, "balance")
    async with renv.session_factory() as session:
        session.add(OrderPayment(order_id=order_id, price_rub=130, balance_rub=130, cash_rub=0))
        await session.commit()

    assert await renv.admin().approve_order(order_id) is False

    assert renv.marzban.count("create_user") == 0


async def test_zero_price_payment_is_not_approved(renv):
    order_id = await _raw_order(renv, "balance")
    async with renv.session_factory() as session:
        session.add(OrderPayment(order_id=order_id, price_rub=0, balance_rub=0, cash_rub=0))
        await session.commit()

    assert await renv.admin().approve_order(order_id) is False


async def test_full_balance_order_is_approved_without_admin_step_and_issues_package(renv):
    await renv.add_balance(USER, 200)
    placed = await renv.orders().place(USER, "u", 50, "balance", balance_rub=200, require_full=True)
    bot = FakeBot()

    assert await renv.admin(bot).approve_order(placed.order.id) is True

    assert (await renv.order(placed.order.id)).status == "completed"
    assert renv.marzban.users[f"user_{USER}_{placed.order.id}"]["data_limit"] == 50 * GB
    assert await renv.ledger_sum(USER) == 70
    assert len(bot.messages) == 1


async def test_full_balance_order_survives_marzban_failure_and_retry_is_idempotent(renv):
    await renv.add_balance(USER, 130)
    placed = await renv.orders().place(USER, "u", 50, "balance", balance_rub=130, require_full=True)
    renv.marzban.fail["create_user"] = 1
    admin = renv.admin()

    assert await admin.approve_order(placed.order.id) is False
    assert (await renv.order(placed.order.id)).status == "pending"
    assert await renv.ledger_sum(USER) == 0  # резерв остаётся

    assert await admin.approve_order(placed.order.id) is True
    assert await admin.approve_order(placed.order.id) is False

    assert await renv.ledger_sum(USER) == 0
    assert len(await renv.entries(USER, BalanceKind.ORDER_SPEND)) == 1
    assert renv.marzban.count("create_user") == 2  # первая попытка упала, вторая создала
    assert len(renv.marzban.users) == 1
