from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from src.adapters.tg_bot.handlers.referral import REF_MENU, REF_NEW, referral_menu_handler
from src.adapters.tg_bot.handlers.start import start_handler
from src.domain.models import BalanceKind, CloseReason, Order, OrderPayment, ReferralLink, ReferralStatus
from src.domain.referral_rules import generate_token
from src.use_cases.referral_use_cases import OverviewStatus
from tests.fakes import GB

A = 20
B = 21
C = 22

NOT_ELIGIBLE_TEXT = "Приглашения доступны после первой покупки."


async def _link_count(renv) -> int:
    async with renv.session_factory() as session:
        return (await session.execute(select(func.count()).select_from(ReferralLink))).scalar_one()


async def _legacy_link(renv, inviter: int) -> str:
    """Ссылка, выпущенная в обход правила (как до его введения): сразу в репозитории."""
    token = generate_token()
    link = await renv.referrals.create_link(inviter, f"user{inviter}", token, renv.clock(), 3)
    assert link is not None
    return token


async def _buy(renv, user_id: int, tariff_gb: int = 50, balance_rub: int = 0) -> int:
    placed = await renv.place(user_id, tariff_gb, balance_rub=balance_rub)
    assert await renv.admin().approve_order(placed.order.id) is True
    return placed.order.id


async def _buy_by_balance(renv, user_id: int, tariff_gb: int = 50) -> int:
    """Покупка целиком с баланса (путь «Оплатить балансом»): денежная часть 0."""
    price = {50: 130, 150: 250, 450: 449}[tariff_gb]
    placed = await renv.orders().place(user_id, f"user{user_id}", tariff_gb, "balance", balance_rub=price, require_full=True)
    assert await renv.admin().approve_order(placed.order.id) is True
    return placed.order.id


async def _insert_order(renv, user_id: int, status: str, cash_rub: int | None = 130) -> int:
    async with renv.session_factory() as session:
        order = Order(user_id=user_id, tariff_gb=50, order_type="new", status=status, photo_file_id="photo")
        session.add(order)
        await session.flush()
        if cash_rub is not None:
            session.add(
                OrderPayment(order_id=order.id, price_rub=130, balance_rub=130 - cash_rub, cash_rub=cash_rub)
            )
        await session.commit()
        return order.id


# ---------- пользователь без оплаты деньгами не может приглашать ----------

async def test_user_without_purchases_cannot_create_or_see_a_link(renv):
    for call in (renv.links.show, renv.links.create):
        overview = await call(A, "a")
        assert overview.status == OverviewStatus.NOT_ELIGIBLE
        assert overview.links == () and overview.new_link is None and overview.can_create is False

    assert (await renv.links.revoke(A, 1)).status == OverviewStatus.NOT_ELIGIBLE
    assert await _link_count(renv) == 0


async def test_previously_issued_links_of_a_non_paying_user_are_not_shown(renv):
    await _legacy_link(renv, A)

    overview = await renv.links.show(A, "a")

    assert overview.status == OverviewStatus.NOT_ELIGIBLE
    assert overview.links == ()


async def test_trial_alone_does_not_give_the_right_to_invite(renv):
    await renv.base.grant(A)

    assert (await renv.links.show(A, "a")).status == OverviewStatus.NOT_ELIGIBLE


@pytest.mark.parametrize("status", ["pending", "rejected", "cancelled"])
async def test_unconfirmed_orders_do_not_give_the_right_to_invite(renv, status):
    await _insert_order(renv, A, status)

    assert (await renv.links.show(A, "a")).status == OverviewStatus.NOT_ELIGIBLE
    assert await renv.referrals.has_cash_paid_order(A) is False


async def test_single_purchase_paid_fully_by_balance_does_not_give_the_right_to_invite(renv):
    await renv.add_balance(A, 200)
    placed = await renv.orders().place(A, "a", 50, "balance", balance_rub=200, require_full=True)
    assert await renv.admin().approve_order(placed.order.id) is True
    assert (await renv.order(placed.order.id)).status == "completed"
    assert (await renv.payment(placed.order.id)).cash_rub == 0

    overview = await renv.links.show(A, "a")

    assert overview.status == OverviewStatus.NOT_ELIGIBLE
    assert await _link_count(renv) == 0


async def test_purchase_paid_partly_by_balance_gives_the_right_to_invite(renv):
    await renv.add_balance(A, 39)
    await _buy(renv, A, 50, balance_rub=39)
    assert (await renv.payment(1)).cash_rub == 91

    overview = await renv.links.show(A, "a")

    assert overview.status == OverviewStatus.OK
    assert len(overview.links) == 1


async def test_order_created_before_the_feature_without_payment_row_counts_as_paid_by_cash(renv):
    await renv.base.add_order(A, "completed")
    assert await renv.payment(1) is None

    assert (await renv.links.show(A, "a")).status == OverviewStatus.OK


async def test_user_gets_the_right_after_the_first_cash_purchase_and_keeps_it(renv):
    assert (await renv.links.show(A, "a")).status == OverviewStatus.NOT_ELIGIBLE

    await _buy(renv, A)

    first = await renv.links.show(A, "a")
    assert first.status == OverviewStatus.OK and len(first.links) == 1
    await renv.add_balance(A, 500)
    await _buy_by_balance(renv, A)  # следующая покупка балансом право не отнимает
    assert (await renv.links.show(A, "a")).status == OverviewStatus.OK


# ---------- сценарий ручного теста: A -> B -> A ----------

async def test_manual_test_scenario_mutual_invitation_is_refused_and_pays_nothing(renv):
    await _buy(renv, A)  # A платит деньгами
    a_link = await renv.links.show(A, "a")
    assert a_link.status == OverviewStatus.OK
    a_token = a_link.links[0].url.split("start=ref_")[1]
    assert (await renv.accept.execute(B, "b", f"ref_{a_token}")).bound is True

    await _buy(renv, B)  # B платит деньгами: A получает награду 39 ₽
    assert await renv.ledger_sum(A) == 39

    b_link = await renv.links.show(B, "b")  # B теперь платящий и может приглашать
    assert b_link.status == OverviewStatus.OK
    b_token = b_link.links[0].url.split("start=ref_")[1]

    # A пытается привязаться к B: он не «новый» (есть платный заказ)
    reply = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=A, username="a"),
        message=SimpleNamespace(reply_text=reply),
        callback_query=None,
    )
    context = SimpleNamespace(
        bot_data={"accept_referral_uc": renv.accept, "referral_config": renv.config},
        args=[f"ref_{b_token}"],
    )
    await start_handler(update, context)

    assert reply.call_args.args[0].startswith("Ссылка недействительна.")
    assert await renv.referral_of(A) is None
    assert await renv.referral_of(B) is not None  # B по-прежнему приглашён A
    async with renv.session_factory() as session:
        assert (await session.execute(select(ReferralLink).where(ReferralLink.token == b_token))).scalar_one().used_at is None

    # дальнейшие покупки A наград B не приносят
    await renv.add_balance(A, 130)
    await _buy_by_balance(renv, A)
    assert await renv.entries(B, BalanceKind.REFERRAL_REWARD) == []
    assert await renv.ledger_sum(B) == 0
    assert await renv.ledger_sum(A) == 39 + 130 - 130  # награда за B + пополнение - списание


async def test_reward_is_never_paid_in_both_directions_even_with_a_link_issued_before_the_rule(renv):
    legacy_token = await _legacy_link(renv, A)  # у A нет оплаты
    assert (await renv.accept.execute(B, "b", f"ref_{legacy_token}")).bound is True

    b_order = await _buy(renv, B)  # B: бонус ГБ есть, награды A нет
    assert (await renv.order(b_order)).planned_data_limit == 65 * GB
    assert await renv.entries(A, BalanceKind.REFERRAL_REWARD) == []

    b_link = await renv.links.show(B, "b")
    assert b_link.status == OverviewStatus.OK
    b_token = b_link.links[0].url.split("start=ref_")[1]
    assert (await renv.accept.execute(A, "a", f"ref_{b_token}")).bound is True  # A ещё «новый»
    await _buy(renv, A)

    # награда ушла только B (за A); у A за B ничего нет
    assert await renv.ledger_sum(B) == 39
    assert await renv.entries(A, BalanceKind.REFERRAL_REWARD) == []
    assert await renv.ledger_sum(A) == 0


# ---------- проверка при начислении награды ----------

async def test_inviter_without_cash_payment_gets_no_reward_but_invitee_gets_bonus(renv):
    token = await _legacy_link(renv, A)
    assert (await renv.accept.execute(B, "b", f"ref_{token}")).bound is True

    order_id = await _buy(renv, B)

    assert (await renv.order(order_id)).planned_data_limit == 65 * GB
    assert renv.marzban.users[f"user_{B}_{order_id}"]["data_limit"] == 65 * GB
    assert await renv.entries(A, BalanceKind.REFERRAL_REWARD) == []
    referral = await renv.referral_of(B)
    assert (referral.status, referral.close_reason, referral.reward_rub) == (
        ReferralStatus.CLOSED, CloseReason.INVITER_NOT_PAID, None
    )
    assert renv.notifier.attempts == 0


async def test_closed_binding_is_not_revived_when_inviter_pays_later(renv):
    token = await _legacy_link(renv, A)
    await renv.accept.execute(B, "b", f"ref_{token}")
    await _buy(renv, B)
    await _buy(renv, A)  # A стал платящим уже после

    await _buy(renv, B, 150)

    assert await renv.entries(A, BalanceKind.REFERRAL_REWARD) == []
    assert (await renv.referral_of(B)).close_reason == CloseReason.INVITER_NOT_PAID


async def test_inviter_whose_only_purchase_was_paid_by_balance_gets_no_reward(renv):
    await renv.make_eligible(A, cash_rub=0)  # единственный заказ A оплачен балансом
    token = await _legacy_link(renv, A)
    await renv.accept.execute(B, "b", f"ref_{token}")

    order_id = await _buy(renv, B)

    assert (await renv.order(order_id)).planned_data_limit == 65 * GB
    assert await renv.entries(A, BalanceKind.REFERRAL_REWARD) == []
    assert (await renv.referral_of(B)).close_reason == CloseReason.INVITER_NOT_PAID


async def test_inviter_who_paid_cash_before_the_confirmation_is_rewarded(renv):
    token = await _legacy_link(renv, A)
    await renv.accept.execute(B, "b", f"ref_{token}")
    placed = await renv.place(B, 50)  # заказ B ждёт подтверждения
    await _buy(renv, A)  # A платит деньгами до подтверждения

    assert await renv.admin().approve_order(placed.order.id) is True

    rewards = await renv.entries(A, BalanceKind.REFERRAL_REWARD)
    assert [e.amount_rub for e in rewards] == [39]
    assert (await renv.referral_of(B)).status == ReferralStatus.REWARDED


async def test_paying_inviter_is_rewarded_as_before(renv):
    await _buy(renv, A)
    token = (await renv.links.show(A, "a")).links[0].url.split("start=ref_")[1]
    await renv.accept.execute(B, "b", f"ref_{token}")

    await _buy(renv, B, 450)

    assert [e.amount_rub for e in await renv.entries(A, BalanceKind.REFERRAL_REWARD)] == [134]


# ---------- экран ----------

def _callback_update(user_id: int, data: str):
    edit_text = AsyncMock()
    query = SimpleNamespace(data=data, answer=AsyncMock(), message=SimpleNamespace(edit_text=edit_text))
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id, username="u"), callback_query=query), edit_text


async def test_invite_screen_for_non_paying_user_is_neutral_and_has_no_link(renv):
    update, edit = _callback_update(A, REF_MENU)
    context = SimpleNamespace(bot_data={"referral_link_uc": renv.links, "referral_config": renv.config})

    await referral_menu_handler(update, context)

    text = edit.call_args.args[0]
    markup = edit.call_args.kwargs["reply_markup"]
    assert text == NOT_ELIGIBLE_TEXT
    assert "<code>" not in text and "t.me" not in text
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    assert REF_NEW not in callbacks and "buy_vpn" in callbacks and "start" in callbacks
    for row in markup.inline_keyboard:
        for button in row:
            assert getattr(button, "switch_inline_query", None) is None
    assert await _link_count(renv) == 0


async def test_invite_button_stays_on_the_start_screen_for_everyone(renv):
    reply = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=C, username="c"), message=SimpleNamespace(reply_text=reply), callback_query=None
    )
    context = SimpleNamespace(bot_data={"referral_config": renv.config}, args=None)

    await start_handler(update, context)

    labels = [b.text for row in reply.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
    assert "Пригласить друга" in labels
