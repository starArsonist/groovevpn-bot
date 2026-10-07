from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.adapters.tg_bot.handlers.start import _days_word, build_welcome_text, start_handler
from src.use_cases.trial_use_cases import TrialOfferUseCase
from tests.fakes import build_env, make_config

PROMO_MARKERS = ("бесплатно", "Попробуйте наш сервис", "Не нужно ничего оплачивать")


def _message_update(user_id: int):
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="t"), message=message, callback_query=None
    )
    return update, message.reply_text


def _callback_update(user_id: int):
    edit_text = AsyncMock()
    query = SimpleNamespace(message=SimpleNamespace(edit_text=edit_text))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="t"), message=None, callback_query=query
    )
    return update, edit_text


async def _start_text(env, user_id: int, **config) -> tuple[str, list[str]]:
    offer = TrialOfferUseCase(env.repo, make_config(**config))
    update, reply = _message_update(user_id)

    await start_handler(update, SimpleNamespace(bot_data={"trial_offer_uc": offer}))

    markup = reply.call_args.kwargs["reply_markup"]
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    return reply.call_args.args[0], callbacks


def _has_promo(text: str) -> bool:
    return any(marker in text for marker in PROMO_MARKERS)


# ---------- кому показывается блок ----------

async def test_new_user_sees_trial_text_and_button(env):
    text, callbacks = await _start_text(env, 1)

    assert text.startswith("🌐 <b>GrooveVPN</b>")
    assert "Попробуйте наш сервис <b>бесплатно</b>: 10 ГБ трафика на 7 дней" in text
    assert "Не нужно ничего оплачивать или привязывать - нажмите на кнопку ниже, сразу выдадим подписку" in text
    assert callbacks[0] == "trial_start"


async def test_user_who_already_took_trial_sees_no_trial_text(env):
    await env.grant(1)

    text, callbacks = await _start_text(env, 1)

    assert not _has_promo(text)
    assert "trial_start" not in callbacks


@pytest.mark.parametrize("order_status", ["completed", "pending"])
async def test_user_with_paid_or_pending_order_sees_no_trial_text(env, order_status):
    await env.add_order(1, order_status)

    text, callbacks = await _start_text(env, 1)

    assert not _has_promo(text)
    assert "trial_start" not in callbacks


async def test_user_with_only_rejected_order_still_sees_trial_text(env):
    await env.add_order(1, "rejected")

    text, callbacks = await _start_text(env, 1)

    assert _has_promo(text)
    assert callbacks[0] == "trial_start"


async def test_trial_disabled_shows_no_trial_text(env):
    text, callbacks = await _start_text(env, 1, enabled=False)

    assert not _has_promo(text)
    assert "trial_start" not in callbacks


async def test_ended_and_converted_trial_users_see_no_trial_text(env):
    await env.grant(1)
    await env.repo.mark_ended((await env.repo.get(1)).id, "time", env.clock.now)
    ended_text, _ = await _start_text(env, 1)

    await env.repo.mark_converted(1, env.clock.now)
    converted_text, _ = await _start_text(env, 1)

    assert not _has_promo(ended_text) and not _has_promo(converted_text)


async def test_daily_cap_reached_still_shows_the_offer(env):
    # лимит исчерпан - человек всё равно "имеет право", ответ про недоступность он получит по кнопке
    text, callbacks = await _start_text(env, 1, daily_cap=0)

    assert _has_promo(text) and callbacks[0] == "trial_start"


# ---------- сбои и отсутствие зависимости ----------

class FailingOffer:
    config = make_config()

    async def is_offered(self, telegram_id):
        raise RuntimeError("db down")


@pytest.mark.parametrize("bot_data", [{}, {"trial_offer_uc": FailingOffer()}])
async def test_without_offer_or_on_error_start_shows_base_text_only(bot_data):
    update, reply = _message_update(1)

    await start_handler(update, SimpleNamespace(bot_data=bot_data))

    text = reply.call_args.args[0]
    assert text == build_welcome_text(None)
    assert not _has_promo(text)
    callbacks = [b.callback_data for row in reply.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
    assert "trial_start" not in callbacks


# ---------- возврат на стартовый экран кнопкой "Назад" ----------

async def test_returning_via_button_applies_the_same_rule(env):
    offer = TrialOfferUseCase(env.repo, make_config())
    context = SimpleNamespace(bot_data={"trial_offer_uc": offer})

    update, edit_text = _callback_update(1)
    await start_handler(update, context)
    assert _has_promo(edit_text.call_args.args[0])

    await env.grant(1)
    update, edit_text = _callback_update(1)
    await start_handler(update, context)
    assert not _has_promo(edit_text.call_args.args[0])


# ---------- текст берётся из конфигурации, а не зашит ----------

async def test_text_follows_trial_configuration(env):
    text, _ = await _start_text(env, 1, data_gb=5, days=3)

    assert "5 ГБ трафика на 3 дня" in text
    assert "10 ГБ" not in text and "7 дней" not in text


@pytest.mark.parametrize(
    "days,word",
    [(1, "день"), (2, "дня"), (3, "дня"), (4, "дня"), (5, "дней"), (7, "дней"), (11, "дней"),
     (12, "дней"), (14, "дней"), (21, "день"), (22, "дня"), (30, "дней"), (101, "день")],
)
def test_days_word_agrees_with_number(days, word):
    assert _days_word(days) == word
