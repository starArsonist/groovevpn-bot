import asyncio
from datetime import datetime

import pytest

from src.adapters.db.order_settlement import CancelOutcome
from src.adapters.db.repositories import UserRepository, VPNProfileRepository
from src.domain.models import BalanceKind, CloseReason, Order, ReferralStatus
from src.domain.referral_rules import ReferralConfig
from src.domain.trial_rules import SendOutcome
from tests.fakes import GB, FakeBot
from tests.referral_fakes import build_ref_env

INVITER = 10
FRIEND = 50


async def _approve(renv, user_id: int, tariff_gb: int = 50, balance_rub: int = 0, bot: FakeBot | None = None) -> int:
    placed = await renv.place(user_id, tariff_gb, balance_rub=balance_rub)
    assert await renv.admin(bot).approve_order(placed.order.id) is True
    return placed.order.id


def _rewards(entries):
    return [(e.user_id, e.amount_rub) for e in entries]


# ---------- бонус приглашённому и награда пригласившему ----------

@pytest.mark.parametrize("tariff_gb,reward", [(50, 39), (150, 75), (450, 134)])
async def test_first_paid_order_gives_bonus_in_plan_and_floor_30_percent_reward(renv, tariff_gb, reward):
    await renv.bind(INVITER, FRIEND)

    order_id = await _approve(renv, FRIEND, tariff_gb)

    saved = await renv.order(order_id)
    assert saved.planned_data_limit == (tariff_gb + 15) * GB
    assert renv.marzban.users[f"user_{FRIEND}_{order_id}"]["data_limit"] == (tariff_gb + 15) * GB
    assert (await renv.payment(order_id)).bonus_bytes == 15 * GB
    rewards = await renv.entries(kind=BalanceKind.REFERRAL_REWARD)
    assert _rewards(rewards) == [(INVITER, reward)]
    referral = await renv.referral_of(FRIEND)
    assert (referral.status, referral.reward_rub) == (ReferralStatus.REWARDED, reward)
    assert rewards[0].ref_id == referral.id


async def test_bonus_size_comes_from_config(env, shared_session):
    renv = build_ref_env(env, shared_session, ReferralConfig(invitee_bonus_gb=7, reward_percent=10))
    await renv.bind(INVITER, FRIEND)
    bot = FakeBot()

    order_id = await _approve(renv, FRIEND, 50, bot=bot)

    assert (await renv.order(order_id)).planned_data_limit == 57 * GB
    assert _rewards(await renv.entries(kind=BalanceKind.REFERRAL_REWARD)) == [(INVITER, 13)]
    assert "+7 ГБ" in bot.messages[0][1]


async def test_issue_message_mentions_bonus_only_when_it_was_given(renv):
    await renv.bind(INVITER, FRIEND)
    bot = FakeBot()
    await _approve(renv, FRIEND, 50, bot=bot)
    assert "Бонус по приглашению: +15 ГБ" in bot.messages[0][1]

    plain_bot = FakeBot()
    await _approve(renv, 77, 50, bot=plain_bot)
    assert "Бонус" not in plain_bot.messages[0][1]


async def test_trial_user_gets_bonus_on_top_of_package_without_trial_carryover(renv):
    await renv.base.grant(FRIEND)
    renv.marzban.set_used(f"user_{FRIEND}_trial", 4 * GB)
    await renv.bind(INVITER, FRIEND)

    order_id = await _approve(renv, FRIEND, 50)

    saved = await renv.order(order_id)
    assert saved.carried_over_bytes == 0
    assert saved.planned_data_limit == 65 * GB
    assert renv.marzban.users[f"user_{FRIEND}_trial"]["data_limit"] == 65 * GB
    assert _rewards(await renv.entries(kind=BalanceKind.REFERRAL_REWARD)) == [(INVITER, 39)]


async def test_bonus_gigabytes_are_carried_over_on_the_next_purchase_as_regular_ones(renv):
    await renv.bind(INVITER, FRIEND)
    first = await _approve(renv, FRIEND, 50)
    name = f"user_{FRIEND}_{first}"
    renv.marzban.set_used(name, 10 * GB)  # из 65 ГБ осталось 55

    second = await _approve(renv, FRIEND, 50)

    saved = await renv.order(second)
    assert saved.carried_over_bytes == 55 * GB
    assert saved.planned_data_limit == 105 * GB  # 50 + 55, без нового бонуса
    assert (await renv.payment(second)).bonus_bytes == 0
    assert len(await renv.entries(kind=BalanceKind.REFERRAL_REWARD)) == 1


async def test_second_purchase_gives_no_new_bonus_or_reward(renv):
    await renv.bind(INVITER, FRIEND)
    await _approve(renv, FRIEND, 50)
    await _approve(renv, FRIEND, 150)

    assert len(await renv.entries(kind=BalanceKind.REFERRAL_REWARD)) == 1
    assert await renv.ledger_sum(INVITER) == 39


async def test_user_without_referral_is_unaffected(renv):
    order_id = await _approve(renv, FRIEND, 50)

    assert (await renv.order(order_id)).planned_data_limit == 50 * GB
    assert await renv.entries() == []


# ---------- повторы и сбои ----------

async def test_repeated_approve_changes_nothing(renv):
    await renv.bind(INVITER, FRIEND)
    placed = await renv.place(FRIEND, 50)
    admin = renv.admin()
    assert await admin.approve_order(placed.order.id) is True
    snapshot = (await renv.order(placed.order.id)).planned_data_limit
    calls = len(renv.marzban.calls)

    assert await admin.approve_order(placed.order.id) is False
    assert await renv.admin().approve_order(placed.order.id) is False  # другой экземпляр, другой lock

    assert (await renv.order(placed.order.id)).planned_data_limit == snapshot
    assert len(renv.marzban.calls) == calls
    assert len(await renv.entries(kind=BalanceKind.REFERRAL_REWARD)) == 1
    assert renv.notifier.attempts <= 1


async def test_marzban_failure_then_retry_gives_bonus_once_and_reward_once(renv):
    await renv.bind(INVITER, FRIEND)
    placed = await renv.place(FRIEND, 50)
    renv.marzban.fail["create_user"] = 2
    admin = renv.admin()

    assert await admin.approve_order(placed.order.id) is False
    saved = await renv.order(placed.order.id)
    assert saved.status == "pending" and saved.planned_data_limit == 65 * GB
    assert await renv.entries(kind=BalanceKind.REFERRAL_REWARD) == []  # награда не раньше подтверждения
    assert (await renv.referral_of(FRIEND)).bonus_order_id == placed.order.id

    assert await admin.approve_order(placed.order.id) is False
    assert await admin.approve_order(placed.order.id) is True

    assert renv.marzban.users[f"user_{FRIEND}_{placed.order.id}"]["data_limit"] == 65 * GB
    assert _rewards(await renv.entries(kind=BalanceKind.REFERRAL_REWARD)) == [(INVITER, 39)]


async def test_topup_failure_after_reset_then_retry_keeps_bonus_and_reward_once(renv):
    await renv.base.grant(FRIEND)
    await renv.bind(INVITER, FRIEND)
    placed = await renv.place(FRIEND, 50)
    renv.marzban.fail["update_user"] = 1
    admin = renv.admin()

    assert await admin.approve_order(placed.order.id) is False
    assert await admin.approve_order(placed.order.id) is True

    assert renv.marzban.count("reset_user_data_usage") == 1
    assert renv.marzban.users[f"user_{FRIEND}_trial"]["data_limit"] == 65 * GB
    assert _rewards(await renv.entries(kind=BalanceKind.REFERRAL_REWARD)) == [(INVITER, 39)]


async def test_failure_of_the_completion_transaction_rolls_back_everything_and_retry_rewards_once(renv):
    await renv.bind(INVITER, FRIEND)
    placed = await renv.place(FRIEND, 50)
    real_complete = renv.settlement.complete
    state = {"failed": False}

    async def flaky_complete(*args, **kwargs):
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("database went away")
        return await real_complete(*args, **kwargs)

    renv.settlement.complete = flaky_complete
    admin = renv.admin()

    assert await admin.approve_order(placed.order.id) is False
    assert (await renv.order(placed.order.id)).status == "pending"
    assert await renv.entries(kind=BalanceKind.REFERRAL_REWARD) == []

    assert await admin.approve_order(placed.order.id) is True

    assert _rewards(await renv.entries(kind=BalanceKind.REFERRAL_REWARD)) == [(INVITER, 39)]
    assert renv.marzban.users[f"user_{FRIEND}_{placed.order.id}"]["data_limit"] == 65 * GB


async def test_reward_and_completion_are_one_transaction_even_if_reward_insert_fails(renv):
    await renv.bind(INVITER, FRIEND)
    placed = await renv.place(FRIEND, 50)
    referral = await renv.referral_of(FRIEND)
    # ключ (kind, ref_id) награды уже занят: вставка награды нарушит UNIQUE
    await renv.add_balance(INVITER, 5, ref_id=referral.id)

    assert await renv.admin().approve_order(placed.order.id) is False

    assert (await renv.order(placed.order.id)).status == "pending"
    assert (await renv.referral_of(FRIEND)).status == ReferralStatus.BOUND


# ---------- лимит наград ----------

async def test_monthly_cap_stops_rewards_but_not_invitee_bonus(renv):
    for i in range(10):
        await renv.bind(INVITER, 100 + i)
        await _approve(renv, 100 + i, 50)
    assert await renv.ledger_sum(INVITER) == 390

    await renv.bind(INVITER, 200)
    order_id = await _approve(renv, 200, 50)

    assert (await renv.order(order_id)).planned_data_limit == 65 * GB
    referral = await renv.referral_of(200)
    assert (referral.status, referral.close_reason) == (ReferralStatus.CLOSED, CloseReason.MONTHLY_CAP)
    assert await renv.ledger_sum(INVITER) == 390
    assert len(await renv.entries(INVITER, BalanceKind.REFERRAL_REWARD)) == 10


async def test_monthly_cap_resets_in_next_calendar_month(renv):
    renv.clock.now = datetime(2026, 10, 30, 12)
    for i in range(10):
        await renv.bind(INVITER, 100 + i)
        await _approve(renv, 100 + i, 50)
    await renv.bind(INVITER, 200)
    await _approve(renv, 200, 50)
    assert len(await renv.entries(INVITER, BalanceKind.REFERRAL_REWARD)) == 10

    renv.clock.now = datetime(2026, 11, 1, 0, 0, 1)
    await renv.bind(INVITER, 201)
    await _approve(renv, 201, 50)

    assert len(await renv.entries(INVITER, BalanceKind.REFERRAL_REWARD)) == 11


async def test_cap_is_per_inviter(renv):
    for i in range(10):
        await renv.bind(INVITER, 100 + i)
        await _approve(renv, 100 + i, 50)

    await renv.bind(11, 300)
    await _approve(renv, 300, 50)

    assert _rewards(await renv.entries(11, BalanceKind.REFERRAL_REWARD)) == [(11, 39)]


async def test_concurrent_completions_of_one_inviters_friends_respect_the_cap(env, shared_session):
    renv = build_ref_env(env, shared_session, ReferralConfig(monthly_cap=2))
    orders = []
    for i in range(4):
        await renv.bind(INVITER, 100 + i)
        orders.append((await renv.place(100 + i, 50)).order.id)
    # завершаем напрямую транзакцией (общая сессия не параллелится), все четыре одновременно
    for i, order_id in enumerate(orders):
        await renv.settlement.claim_invitee_bonus(order_id, 100 + i, 15 * GB, renv.clock(), True)

    results = await asyncio.gather(*(renv.settlement.complete(o, renv.clock(), renv.config) for o in orders))

    assert sum(1 for r in results if r.reward is not None) == 2
    assert len(await renv.entries(INVITER, BalanceKind.REFERRAL_REWARD)) == 2


# ---------- оплата балансом ----------

async def test_first_order_paid_fully_by_balance_gives_no_bonus_and_no_reward(renv):
    await renv.bind(INVITER, FRIEND)
    await renv.add_balance(FRIEND, 200)
    placed = await renv.orders().place(FRIEND, "u", 50, "balance", balance_rub=200, require_full=True)

    assert await renv.admin().approve_order(placed.order.id) is True

    assert (await renv.order(placed.order.id)).planned_data_limit == 50 * GB
    assert (await renv.payment(placed.order.id)).bonus_bytes == 0
    assert await renv.ledger_sum(INVITER) == 0
    referral = await renv.referral_of(FRIEND)
    assert (referral.status, referral.close_reason) == (ReferralStatus.CLOSED, CloseReason.NO_CASH)
    assert renv.notifier.attempts == 0

    # привязка закрыта: следующая покупка деньгами бонуса уже не даёт
    second = await _approve(renv, FRIEND, 50)
    assert (await renv.order(second)).planned_data_limit != 65 * GB
    assert await renv.ledger_sum(INVITER) == 0


async def test_partial_balance_reduces_reward_base_to_cash_part(renv):
    await renv.bind(INVITER, FRIEND)
    await renv.add_balance(FRIEND, 39)

    order_id = await _approve(renv, FRIEND, 50, balance_rub=39)

    assert (await renv.order(order_id)).planned_data_limit == 65 * GB  # бонус есть: деньги платились
    assert _rewards(await renv.entries(INVITER, BalanceKind.REFERRAL_REWARD)) == [(INVITER, 27)]  # 91 * 30 // 100
    assert await renv.ledger_sum(FRIEND) == 0


async def test_reward_that_rounds_to_zero_closes_binding_but_keeps_invitee_bonus(renv):
    await renv.bind(INVITER, FRIEND)
    await renv.add_balance(FRIEND, 127)  # денег остаётся 3 ₽: 3 * 30 // 100 = 0

    order_id = await _approve(renv, FRIEND, 50, balance_rub=127)

    assert (await renv.order(order_id)).planned_data_limit == 65 * GB
    assert await renv.entries(INVITER) == []
    assert (await renv.referral_of(FRIEND)).close_reason == CloseReason.NO_REWARD


# ---------- несколько заказов приглашённого ----------

async def test_bonus_and_reward_go_to_the_order_that_computed_its_plan_first(renv):
    await renv.bind(INVITER, FRIEND)
    a = await renv.place(FRIEND, 50)
    b = await renv.place(FRIEND, 150)
    admin = renv.admin()

    assert await admin.approve_order(b.order.id) is True
    assert await admin.approve_order(a.order.id) is True

    assert (await renv.order(b.order.id)).planned_data_limit == 165 * GB
    assert (await renv.order(a.order.id)).planned_data_limit != 65 * GB
    assert _rewards(await renv.entries(INVITER, BalanceKind.REFERRAL_REWARD)) == [(INVITER, 75)]


async def test_rejecting_the_claiming_order_releases_the_claim(renv):
    await renv.bind(INVITER, FRIEND)
    a = await renv.place(FRIEND, 50)
    b = await renv.place(FRIEND, 50)
    renv.marzban.fail["create_user"] = 1
    admin = renv.admin()
    assert await admin.approve_order(a.order.id) is False  # заявка на бонус у заказа A
    assert (await renv.referral_of(FRIEND)).bonus_order_id == a.order.id

    assert await admin.reject_order(a.order.id) is True
    assert (await renv.referral_of(FRIEND)).bonus_order_id is None

    assert await admin.approve_order(b.order.id) is True
    assert (await renv.order(b.order.id)).planned_data_limit == 65 * GB
    assert _rewards(await renv.entries(INVITER, BalanceKind.REFERRAL_REWARD)) == [(INVITER, 39)]


async def test_cancelling_the_claiming_order_releases_the_claim(renv):
    await renv.bind(INVITER, FRIEND)
    a = await renv.place(FRIEND, 50)
    # заявка ставится при расчёте плана, до вызовов Marzban
    await renv.settlement.claim_invitee_bonus(a.order.id, FRIEND, 15 * GB, renv.clock(), True)

    assert await renv.settlement.cancel(a.order.id, FRIEND, renv.clock()) is CancelOutcome.CANCELLED
    assert (await renv.referral_of(FRIEND)).bonus_order_id is None


async def test_unlimited_client_gets_no_bonus_gigabytes_but_inviter_is_still_rewarded(renv):
    await UserRepository(renv.shared).get_or_create(FRIEND, "friend")
    await VPNProfileRepository(renv.shared).create(
        user_id=FRIEND, marzban_username="user_50_old", sub_url="https://sub.test/old", status="active"
    )
    await renv.marzban.create_user("user_50_old", 0)  # безлимитный клиент
    renv.marzban.users["user_50_old"]["data_limit"] = 0
    await renv.bind(INVITER, FRIEND)

    order_id = await _approve(renv, FRIEND, 50)

    saved = await renv.order(order_id)
    assert saved.planned_data_limit is None
    assert (await renv.payment(order_id)).bonus_bytes == 0
    assert _rewards(await renv.entries(INVITER, BalanceKind.REFERRAL_REWARD)) == [(INVITER, 39)]


async def test_legacy_order_without_payment_row_is_treated_as_full_cash_payment(renv):
    await renv.bind(INVITER, FRIEND)
    async with renv.session_factory() as session:
        order = Order(user_id=FRIEND, tariff_gb=150, order_type="new", status="pending", photo_file_id="photo")
        session.add(order)
        await session.commit()
        order_id = order.id
    assert await renv.payment(order_id) is None

    assert await renv.admin().approve_order(order_id) is True

    payment = await renv.payment(order_id)
    assert (payment.price_rub, payment.cash_rub, payment.balance_rub) == (250, 250, 0)
    assert _rewards(await renv.entries(INVITER, BalanceKind.REFERRAL_REWARD)) == [(INVITER, 75)]


# ---------- выключенная фича ----------

async def test_disabled_feature_gives_no_bonus_and_closes_binding_lazily_on_first_purchase(env, shared_session):
    on = build_ref_env(env, shared_session)
    await on.bind(INVITER, FRIEND)
    await on.bind(INVITER, 51)
    off = build_ref_env(env, shared_session, ReferralConfig(enabled=False))

    # флаг выключен: привязки сами по себе не меняются
    assert (await off.referral_of(FRIEND)).status == ReferralStatus.BOUND
    placed = await off.place(FRIEND, 50)
    assert await off.admin().approve_order(placed.order.id) is True

    assert (await off.order(placed.order.id)).planned_data_limit == 50 * GB
    assert await off.entries(INVITER) == []
    closed = await off.referral_of(FRIEND)
    assert (closed.status, closed.close_reason) == (ReferralStatus.CLOSED, CloseReason.NOT_ELIGIBLE)
    assert (await off.referral_of(51)).status == ReferralStatus.BOUND  # чужая привязка не затронута

    # после включения флага уже закрытая привязка наград не даёт
    again = await on.place(FRIEND, 50)
    assert await on.admin().approve_order(again.order.id) is True
    assert await on.entries(INVITER) == []


async def test_claim_made_before_disabling_is_honoured_at_completion(env, shared_session):
    on = build_ref_env(env, shared_session)
    await on.bind(INVITER, FRIEND)
    placed = await on.place(FRIEND, 50)
    on.marzban.fail["create_user"] = 1
    assert await on.admin().approve_order(placed.order.id) is False  # заявка и бонус в плане
    off = build_ref_env(env, shared_session, ReferralConfig(enabled=False))

    assert await off.admin().approve_order(placed.order.id) is True

    assert (await off.order(placed.order.id)).planned_data_limit == 65 * GB
    assert _rewards(await off.entries(INVITER, BalanceKind.REFERRAL_REWARD)) == [(INVITER, 39)]


async def test_existing_balance_is_spendable_when_feature_is_disabled(env, shared_session):
    off = build_ref_env(env, shared_session, ReferralConfig(enabled=False))
    await off.add_balance(FRIEND, 200)

    quote = await off.quote.quote(FRIEND, 50)
    assert (quote.balance_rub, quote.cash_rub, quote.covers_fully) == (130, 0, True)
    placed = await off.orders().place(FRIEND, "u", 50, "balance", balance_rub=130, require_full=True)
    assert await off.admin().approve_order(placed.order.id) is True
    assert await off.ledger_sum(FRIEND) == 70


# ---------- уведомление пригласившему ----------

async def test_inviter_is_notified_with_reward_and_balance(renv):
    await renv.add_balance(INVITER, 10)
    await renv.bind(INVITER, FRIEND)

    await _approve(renv, FRIEND, 50)

    assert renv.notifier.sent == [(INVITER, 39, 49)]
    assert (await renv.referral_of(FRIEND)).notified_at is not None


async def test_notification_is_sent_once_even_if_triggered_repeatedly(renv):
    await renv.bind(INVITER, FRIEND)
    await _approve(renv, FRIEND, 50)
    referral = await renv.referral_of(FRIEND)

    await renv.reward_notifier.notify(referral.id)
    await renv.reward_notifier.run_once()

    assert len(renv.notifier.sent) == 1


async def test_blocked_bot_marks_referral_and_is_not_retried(renv):
    renv.notifier.outcomes = [SendOutcome.BLOCKED]
    await renv.bind(INVITER, FRIEND)

    await _approve(renv, FRIEND, 50)
    await renv.reward_notifier.run_once()
    await renv.reward_notifier.run_once()

    referral = await renv.referral_of(FRIEND)
    assert referral.notify_blocked_at is not None
    assert renv.notifier.attempts == 1
    assert await renv.ledger_sum(INVITER) == 39  # баланс начислен несмотря на блокировку


async def test_temporary_failure_is_retried_by_background_sweep(renv):
    renv.notifier.outcomes = [SendOutcome.FAILED, SendOutcome.FAILED]
    await renv.bind(INVITER, FRIEND)

    await _approve(renv, FRIEND, 50)
    assert renv.notifier.sent == []
    assert (await renv.referral_of(FRIEND)).notified_at is None

    await renv.reward_notifier.run_once()  # вторая неудача
    await renv.reward_notifier.run_once()  # успех
    await renv.reward_notifier.run_once()  # больше не шлём

    assert renv.notifier.sent == [(INVITER, 39, 39)]


async def test_notification_failure_never_breaks_approval(renv):
    class Exploding:
        async def send_reward(self, *args, **kwargs):
            raise RuntimeError("telegram is down")

    renv.reward_notifier.notifier = Exploding()
    await renv.bind(INVITER, FRIEND)
    placed = await renv.place(FRIEND, 50)

    assert await renv.admin().approve_order(placed.order.id) is True

    assert (await renv.order(placed.order.id)).status == "completed"
    assert await renv.ledger_sum(INVITER) == 39
