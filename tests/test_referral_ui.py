from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardMarkup
from telegram.ext import ConversationHandler

from src.adapters.db.repositories import UserRepository
from src.adapters.tg_bot.handlers.admin import (
    admin_decision_handler,
    balance_admin_handler,
    render_balance_overview,
)
from src.adapters.tg_bot.handlers.payment import (
    PAY_BALANCE,
    WAITING_FOR_RECEIPT,
    cancel_order_handler,
    pay_balance_handler,
    payment_details_handler,
    receipt_photo_handler,
    render_payment_screen,
)
from src.adapters.tg_bot.handlers.referral import (
    REF_MENU,
    REF_NEW,
    REF_REVOKE_PREFIX,
    referral_menu_handler,
    referral_new_handler,
    referral_revoke_handler,
    render_overview,
)
from src.adapters.tg_bot.handlers.start import start_handler
from src.adapters.tg_bot.handlers.subscription import my_subscription_handler
from src.adapters.tg_bot.notifier import build_reward_text
from src.domain.models import BalanceKind, Order
from src.domain.referral_rules import ReferralConfig
from src.use_cases.referral_use_cases import OverviewStatus
from tests.fakes import GB
from tests.referral_fakes import build_ref_env

ADMIN = 999
USER = 111
INVITER = 10

FORBIDDEN_WORDS = ("заработ", "доход", "зарабатыв", "рейтинг", "лидер", "топ ", "поделиться")


# ---------- общие помощники ----------

def _plain(entries):
    return [(e.id, e.user_id, e.kind, e.amount_rub, e.ref_id) for e in entries]


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def _callbacks(markup):
    return [b.callback_data for b in _buttons(markup) if b.callback_data]


def _labels(markup):
    return [b.text for b in _buttons(markup)]


def _assert_neutral(text: str, markup=None):
    lowered = text.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered, word
    if markup is not None:
        for button in _buttons(markup):
            assert getattr(button, "switch_inline_query", None) is None
            assert getattr(button, "switch_inline_query_current_chat", None) is None
            assert "поделиться" not in button.text.lower()


def _query(data: str):
    edit_text = AsyncMock()
    query = SimpleNamespace(data=data, answer=AsyncMock(), message=SimpleNamespace(edit_text=edit_text))
    return query, edit_text


def _callback_update(user_id: int, data: str, username: str = "tester"):
    query, edit_text = _query(data)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username=username), callback_query=query, message=None
    )
    return update, edit_text


def _context(renv, user_data: dict | None = None, args: list[str] | None = None, **bot_data):
    data = dict(
        config=SimpleNamespace(telegram_admin_id=ADMIN),
        referral_config=renv.config,
        referral_link_uc=renv.links,
        accept_referral_uc=renv.accept,
        quote_uc=renv.quote,
        create_order_uc=renv.orders(),
        admin_uc=renv.admin(),
        balance_repo=renv.balance,
        balance_overview_uc=renv.balance_overview,
    )
    data.update(bot_data)
    bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
    return SimpleNamespace(bot_data=data, user_data=user_data if user_data is not None else {}, args=args, bot=bot)


def _start_update(user_id: int):
    reply = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="t"), message=SimpleNamespace(reply_text=reply), callback_query=None
    )
    return update, reply


# ---------- /start с ссылкой ----------

async def test_start_with_valid_link_binds_and_shows_bonus_from_config(env, shared_session):
    renv = build_ref_env(env, shared_session, ReferralConfig(invitee_bonus_gb=7))
    link = await renv.make_link(INVITER)
    update, reply = _start_update(USER)

    await start_handler(update, _context(renv, args=[f"ref_{link.token}"]))

    text = reply.call_args.args[0]
    assert text.startswith("Приглашение принято: к первому пакету добавим 7 ГБ.")
    assert "GrooveVPN" in text  # обычный стартовый экран продолжается
    assert (await renv.referral_of(USER)).inviter_id == INVITER


@pytest.mark.parametrize("make_arg", [
    lambda link: "ref_" + "A" * 22,            # нет такой ссылки
    lambda link: f"ref_{link.token}",          # будет уже использована
    lambda link: "ref_garbage!",
])
async def test_start_with_bad_link_shows_one_neutral_line_and_normal_start(renv, make_arg):
    link = await renv.make_link(INVITER)
    await renv.accept.execute(55, "someone", f"ref_{link.token}")  # ссылка использована
    update, reply = _start_update(USER)

    await start_handler(update, _context(renv, args=[make_arg(link)]))

    text = reply.call_args.args[0]
    assert text.startswith("Ссылка недействительна.")
    assert "GrooveVPN" in text
    assert await renv.referral_of(USER) is None
    _assert_neutral(text)


async def test_reasons_for_failure_look_identical_to_the_user(renv):
    own = await renv.make_link(INVITER)
    used = await renv.make_link(INVITER)
    await renv.accept.execute(55, "x", f"ref_{used.token}")
    revoked = await renv.make_link(INVITER)
    await renv.links.revoke(INVITER, revoked.id)
    texts = set()
    for user_id, token in ((INVITER, own.token), (USER, used.token), (USER, revoked.token), (USER, "N" * 22)):
        update, reply = _start_update(user_id)
        await start_handler(update, _context(renv, args=[f"ref_{token}"]))
        texts.add(reply.call_args.args[0].split("\n")[0])
    assert texts == {"Ссылка недействительна."}


async def test_start_without_payload_or_with_foreign_payload_has_no_notice(renv):
    for args in (None, [], ["something_else"]):
        update, reply = _start_update(USER)
        await start_handler(update, _context(renv, args=args))
        text = reply.call_args.args[0]
        assert "Приглашение" not in text and "недействительна" not in text


async def test_start_screen_has_invite_button_between_subscription_and_support(renv):
    update, reply = _start_update(USER)

    await start_handler(update, _context(renv))

    markup = reply.call_args.kwargs["reply_markup"]
    labels = _labels(markup)
    assert labels.index("Моя подписка") < labels.index("Пригласить друга") < labels.index("Поддержка")
    assert REF_MENU in _callbacks(markup)


async def test_start_screen_hides_invite_button_when_feature_is_disabled(env, shared_session):
    renv = build_ref_env(env, shared_session, ReferralConfig(enabled=False))
    update, reply = _start_update(USER)

    await start_handler(update, _context(renv))

    assert "Пригласить друга" not in _labels(reply.call_args.kwargs["reply_markup"])


async def test_start_with_link_while_disabled_binds_nobody(env, shared_session):
    on = build_ref_env(env, shared_session)
    link = await on.make_link(INVITER)
    off = build_ref_env(env, shared_session, ReferralConfig(enabled=False))
    update, reply = _start_update(USER)

    await start_handler(update, _context(off, args=[f"ref_{link.token}"]))

    assert reply.call_args.args[0].startswith("Ссылка недействительна.")
    assert await on.referral_of(USER) is None


# ---------- экран «Пригласить друга» ----------

async def test_invite_screen_shows_link_in_code_and_never_a_share_button(renv):
    await renv.add_balance(INVITER, 39)
    update, edit = _callback_update(INVITER, REF_MENU)

    await referral_menu_handler(update, _context(renv))

    text = edit.call_args.args[0]
    markup = edit.call_args.kwargs["reply_markup"]
    assert "<code>https://t.me/groove_test_bot?start=ref_" in text
    assert "пока не использована" in text
    assert "Приглашено друзей: 0" in text
    assert "Баланс: 39 ₽ (можно потратить только на пакеты)" in text
    assert "15 ГБ" in text and "30%" in text
    assert edit.call_args.kwargs["parse_mode"] == "HTML"
    _assert_neutral(text, markup)
    assert REF_NEW in _callbacks(markup)
    assert any(c.startswith(REF_REVOKE_PREFIX) for c in _callbacks(markup))


async def test_invite_screen_new_link_revoke_and_limit(renv):
    ctx = _context(renv)
    update, edit = _callback_update(INVITER, REF_MENU)
    await referral_menu_handler(update, ctx)
    for _ in range(2):
        update, edit = _callback_update(INVITER, REF_NEW)
        await referral_new_handler(update, ctx)
    markup = edit.call_args.kwargs["reply_markup"]
    assert REF_NEW not in _callbacks(markup)  # три ссылки: слотов нет
    assert edit.call_args.args[0].count("<code>") == 3
    assert "Отозвать ссылку 3" in _labels(markup)

    victim = [c for c in _callbacks(markup) if c.startswith(REF_REVOKE_PREFIX)][0]
    update, edit = _callback_update(INVITER, victim)
    await referral_revoke_handler(update, ctx)

    assert edit.call_args.args[0].count("<code>") == 2
    assert REF_NEW in _callbacks(edit.call_args.kwargs["reply_markup"])


async def test_invite_screen_when_disabled_and_when_bot_name_unknown(env, shared_session):
    off = build_ref_env(env, shared_session, ReferralConfig(enabled=False))
    update, edit = _callback_update(INVITER, REF_MENU)
    await referral_menu_handler(update, _context(off))
    assert edit.call_args.args[0] == "Приглашения сейчас недоступны."

    on = build_ref_env(env, shared_session)
    on.username.username = None
    update, edit = _callback_update(INVITER, REF_MENU)
    await referral_menu_handler(update, _context(on))
    assert "Попробуйте позже" in edit.call_args.args[0]
    assert "start=ref_" not in edit.call_args.args[0]


def test_render_overview_has_no_forbidden_wording(renv):
    overview = SimpleNamespace(
        status=OverviewStatus.OK, links=(SimpleNamespace(id=1, url="https://t.me/b?start=ref_x"),),
        invited=3, balance=12, can_create=True,
    )
    text, markup = render_overview(overview, renv.config)
    _assert_neutral(text, markup)


def test_reward_notification_text_is_neutral_and_informative():
    text = build_reward_text(39, 49)
    assert "Друг оплатил пакет" in text and "Начислено 39 ₽" in text and "баланс 49 ₽" in text
    assert "только на пакеты" in text
    _assert_neutral(text)


# ---------- экран оплаты ----------

async def test_payment_screen_without_balance_keeps_the_original_text(renv):
    update, edit = _callback_update(USER, "tariff_50")
    ctx = _context(renv)

    state = await payment_details_handler(update, ctx)

    assert state == WAITING_FOR_RECEIPT
    text = edit.call_args.args[0]
    assert text.startswith("<b>50 ГБ - 130 ₽</b>")
    assert "Переведите сумму на карту Т-Банка" in text and "Баланс" not in text
    assert ctx.user_data["quoted_balance"] == 0


async def test_payment_screen_shows_balance_spent_and_amount_left_to_pay(renv):
    await renv.add_balance(USER, 39)
    update, edit = _callback_update(USER, "tariff_50")
    ctx = _context(renv)

    await payment_details_handler(update, ctx)

    text = edit.call_args.args[0]
    assert "Списывается с баланса: 39 ₽" in text
    assert "К оплате: <b>91 ₽</b>" in text
    assert "Переведите 91 ₽ на карту Т-Банка" in text
    assert ctx.user_data["quoted_balance"] == 39 and ctx.user_data["quoted_cash"] == 91
    assert PAY_BALANCE not in _callbacks(edit.call_args.kwargs["reply_markup"])


async def test_payment_screen_with_full_cover_offers_pay_by_balance_button(renv):
    await renv.add_balance(USER, 500)
    update, edit = _callback_update(USER, "tariff_50")

    await payment_details_handler(update, _context(renv))

    text = edit.call_args.args[0]
    assert "К оплате: <b>0 ₽</b>" in text and "Перевод не нужен" in text
    assert "+7 960" not in text  # реквизиты не показываются
    assert PAY_BALANCE in _callbacks(edit.call_args.kwargs["reply_markup"])


async def test_payment_screen_mentions_invitee_bonus_only_when_due(renv):
    update, edit = _callback_update(USER, "tariff_50")
    await payment_details_handler(update, _context(renv))
    assert "Бонус по приглашению" not in edit.call_args.args[0]

    await renv.bind(INVITER, USER)
    update, edit = _callback_update(USER, "tariff_50")
    await payment_details_handler(update, _context(renv))
    assert "Бонус по приглашению: +15 ГБ к этому пакету" in edit.call_args.args[0]


def test_render_payment_screen_is_neutral(renv):
    text, markup = render_payment_screen(
        50, 130, SimpleNamespace(balance_rub=39, balance_total=39, cash_rub=91, bonus_gb=15, covers_fully=False)
    )
    _assert_neutral(text, markup)


# ---------- создание заказа из экрана оплаты ----------

def _photo_update(user_id: int):
    reply = AsyncMock()
    message = SimpleNamespace(photo=[SimpleNamespace(file_id="receipt")], reply_text=reply)
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id, username="t"), message=message), reply


async def test_receipt_creates_order_with_hold_and_tells_admin_and_user_the_amounts(renv):
    await renv.add_balance(USER, 39)
    ctx = _context(renv, {"selected_tariff": 50, "quoted_balance": 39, "quoted_cash": 91})
    update, reply = _photo_update(USER)

    result = await receipt_photo_handler(update, ctx)

    assert result == ConversationHandler.END
    caption = ctx.bot.send_photo.call_args.kwargs["caption"]
    assert "Списано с баланса: 39 ₽" in caption and "К получению: <b>91 ₽</b>" in caption
    user_text = reply.call_args.args[0]
    assert "Списано с баланса: 39 ₽. К оплате: 91 ₽." in user_text
    assert "Отменить заказ" in _labels(reply.call_args.kwargs["reply_markup"])
    assert await renv.balance.get_balance(USER) == 0
    assert ctx.user_data == {}


async def test_receipt_warns_when_balance_changed_after_the_screen(renv):
    await renv.add_balance(USER, 20)
    ctx = _context(renv, {"selected_tariff": 50, "quoted_balance": 39, "quoted_cash": 91})
    update, reply = _photo_update(USER)

    await receipt_photo_handler(update, ctx)

    assert "Баланс изменился: сумма к оплате 110 ₽" in reply.call_args.args[0]


async def test_receipt_is_not_accepted_when_balance_covers_the_price(renv):
    await renv.add_balance(USER, 500)
    ctx = _context(renv, {"selected_tariff": 50, "quoted_balance": 130, "quoted_cash": 0})
    update, reply = _photo_update(USER)

    result = await receipt_photo_handler(update, ctx)

    assert result == WAITING_FOR_RECEIPT
    assert ctx.bot.send_photo.await_count == 0
    assert await renv.order(1) is None
    assert PAY_BALANCE in _callbacks(reply.call_args.kwargs["reply_markup"])


async def test_pay_by_balance_issues_package_without_admin_step(renv):
    await renv.add_balance(USER, 200)
    ctx = _context(renv, {"selected_tariff": 50, "quoted_balance": 130, "quoted_cash": 0})
    update, edit = _callback_update(USER, PAY_BALANCE)

    result = await pay_balance_handler(update, ctx)

    assert result == ConversationHandler.END
    assert "оплачен с баланса" in edit.call_args.args[0]
    assert (await renv.order(1)).status == "completed"
    assert (await renv.order(1)).photo_file_id == "balance"
    assert f"user_{USER}_1" in renv.marzban.users
    assert await renv.ledger_sum(USER) == 70
    ctx.bot.send_message.assert_not_awaited()  # администратор не вовлекается


async def test_pay_by_balance_twice_creates_one_order(renv):
    await renv.add_balance(USER, 500)
    ctx = _context(renv, {"selected_tariff": 50, "quoted_balance": 130, "quoted_cash": 0})

    await pay_balance_handler(_callback_update(USER, PAY_BALANCE)[0], ctx)
    update, edit = _callback_update(USER, PAY_BALANCE)
    await pay_balance_handler(update, ctx)

    assert await renv.order(2) is None
    assert "Пакет не выбран" in edit.call_args.args[0]
    assert await renv.ledger_sum(USER) == 370


async def test_pay_by_balance_refuses_when_balance_no_longer_covers_the_price(renv):
    await renv.add_balance(USER, 100)
    ctx = _context(renv, {"selected_tariff": 50, "quoted_balance": 130, "quoted_cash": 0})
    update, edit = _callback_update(USER, PAY_BALANCE)

    await pay_balance_handler(update, ctx)

    assert "Баланс изменился" in edit.call_args.args[0]
    assert await renv.order(1) is None
    assert await renv.balance.get_balance(USER) == 100


async def test_pay_by_balance_marzban_failure_keeps_reserve_and_sends_admin_card(renv):
    await renv.add_balance(USER, 200)
    renv.marzban.fail["create_user"] = 1
    ctx = _context(renv, {"selected_tariff": 50, "quoted_balance": 130, "quoted_cash": 0})
    update, edit = _callback_update(USER, PAY_BALANCE)

    await pay_balance_handler(update, ctx)

    assert "выдача задерживается" in edit.call_args.args[0]
    assert "Отменить заказ" in _labels(edit.call_args.kwargs["reply_markup"])
    card = ctx.bot.send_message.call_args.kwargs
    assert card["chat_id"] == ADMIN and "Оплата балансом" in card["text"]
    assert _callbacks(card["reply_markup"]) == ["approve_1", "reject_1"]
    assert (await renv.order(1)).status == "pending"
    assert await renv.ledger_sum(USER) == 70  # резерв остаётся

    # повтор идемпотентен: админ подтверждает карточку, пакет выдаётся один раз, баланс не меняется
    assert await ctx.bot_data["admin_uc"].approve_order(1) is True
    assert await renv.ledger_sum(USER) == 70
    assert len(renv.marzban.users) == 1


# ---------- отмена заказа пользователем ----------

async def test_user_cancels_own_order_and_gets_balance_back(renv):
    await renv.add_balance(USER, 100)
    placed = await renv.place(USER, 50, balance_rub=100)
    update, edit = _callback_update(USER, f"cancel_order_{placed.order.id}")

    await cancel_order_handler(update, _context(renv))

    assert "Заказ отменён" in edit.call_args.args[0]
    assert (await renv.order(placed.order.id)).status == "cancelled"
    assert await renv.balance.get_balance(USER) == 100


async def test_cancel_foreign_or_processed_order_is_refused_politely(renv):
    placed = await renv.place(USER, 50)

    update, edit = _callback_update(222, f"cancel_order_{placed.order.id}")
    await cancel_order_handler(update, _context(renv))
    assert edit.call_args.args[0] == "Заказ не найден."

    await renv.admin().approve_order(placed.order.id)
    update, edit = _callback_update(USER, f"cancel_order_{placed.order.id}")
    await cancel_order_handler(update, _context(renv))
    assert "уже обработан" in edit.call_args.args[0]


# ---------- «Моя подписка» ----------

class _TrafficUC:
    def __init__(self, data):
        self.data = data

    async def execute(self, user_id):
        return self.data


async def test_subscription_screen_shows_balance_only_when_positive(renv):
    data = dict(used_gb=1.0, limit_gb=50.0, remaining_gb=49.0, is_unlimited=False, status="active", expire_at=None, sub_url="https://s.test/x")
    for traffic in (data, None):
        update, edit = _callback_update(USER, "my_subscription")
        await my_subscription_handler(update, _context(renv, traffic_uc=_TrafficUC(traffic)))
        assert "Баланс" not in edit.call_args.args[0]

    await renv.add_balance(USER, 39)
    for traffic in (data, None):
        update, edit = _callback_update(USER, "my_subscription")
        await my_subscription_handler(update, _context(renv, traffic_uc=_TrafficUC(traffic)))
        assert "Баланс: <b>39 ₽</b> - можно потратить только на пакеты" in edit.call_args.args[0]


# ---------- карточка администратора и /balance ----------

def _admin_query(data: str, *, caption=None, text_html=None, photo=None):
    message = SimpleNamespace(caption=caption, text_html=text_html, photo=photo, reply_text=AsyncMock())
    query = SimpleNamespace(
        data=data, answer=AsyncMock(), message=message,
        edit_message_caption=AsyncMock(), edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN), callback_query=query), query


async def test_admin_decision_edits_caption_of_photo_cards_and_text_of_balance_cards(renv):
    first = await renv.place(USER, 50)
    update, query = _admin_query(f"approve_{first.order.id}", caption="карточка", photo=[object()])
    await admin_decision_handler(update, _context(renv))
    assert "Подтверждено" in query.edit_message_caption.call_args.kwargs["caption"]
    query.edit_message_text.assert_not_awaited()

    second = await renv.place(USER, 50)
    update, query = _admin_query(f"reject_{second.order.id}", text_html="<b>Оплата балансом</b>")
    await admin_decision_handler(update, _context(renv))
    assert "Отклонено" in query.edit_message_text.call_args.kwargs["text"]
    query.edit_message_caption.assert_not_awaited()


def _command_update(user_id: int):
    reply = AsyncMock()
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), message=SimpleNamespace(reply_text=reply)), reply


async def test_balance_command_shows_balance_reserves_and_entries_without_changing_anything(renv):
    await renv.add_balance(USER, 100)
    placed = await renv.place(USER, 50, balance_rub=60)
    before = _plain(await renv.entries())
    update, reply = _command_update(ADMIN)

    await balance_admin_handler(update, _context(renv, args=[str(USER)]))

    text = reply.call_args.args[0]
    assert f"<b>Баланс ID {USER}</b>: 40 ₽" in text
    assert f"#{placed.order.id} - 60 ₽" in text
    assert "order_spend" in text and "-60 ₽" in text and "referral_reward" in text
    assert _plain(await renv.entries()) == before  # только просмотр


async def test_balance_command_is_admin_only_and_validates_arguments(renv):
    update, reply = _command_update(USER)
    await balance_admin_handler(update, _context(renv, args=[str(USER)]))
    assert "нет прав" in reply.call_args.args[0]

    for args in ([], ["abc"], ["1", "2"]):
        update, reply = _command_update(ADMIN)
        await balance_admin_handler(update, _context(renv, args=args))
        assert "Использование" in reply.call_args.args[0]


async def test_balance_command_has_no_adjustment_capability(renv):
    update, reply = _command_update(ADMIN)

    await balance_admin_handler(update, _context(renv, args=[str(USER), "+500"]))

    assert "Использование" in reply.call_args.args[0]
    assert await renv.entries() == []
