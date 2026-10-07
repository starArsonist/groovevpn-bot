from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from src.adapters.marzban.client import MarzbanClient
from src.adapters.marzban.inbounds import build_inbounds_payload
from src.adapters.tg_bot.handlers.start import start_handler
from src.adapters.tg_bot.handlers.trial import render_trial_result, trial_start_handler
from src.adapters.tg_bot.notifier import TelegramTrialNotifier, build_notification_text, tariff_keyboard
from src.config import Settings
from src.domain.trial_rules import SendOutcome, TrialNotificationKind as Kind
from src.use_cases.trial_use_cases import TrialActivationResult, TrialOfferUseCase, TrialResultKind
from tests.fakes import GB, make_config


# ---------- Marzban client / inbounds ----------

async def test_get_users_splits_requests_into_batches_of_50():
    client = MarzbanClient("http://marzban.test", "admin", "secret")
    requests: list[dict] = []

    async def fake_request(method, endpoint, **kwargs):
        requests.append({"method": method, "endpoint": endpoint, **kwargs})
        return {"users": [{"username": name} for name in kwargs["params"]["username"]], "total": 0}

    client._request = fake_request
    names = [f"user_{i}_trial" for i in range(120)]

    users = await client.get_users(names)

    assert [len(r["params"]["username"]) for r in requests] == [50, 50, 20]
    assert all(r["method"] == "GET" and r["endpoint"] == "/api/users" for r in requests)
    assert all(r["params"]["limit"] == len(r["params"]["username"]) for r in requests)
    assert [u["username"] for u in users] == names


async def test_get_users_with_no_names_makes_no_requests():
    client = MarzbanClient("http://marzban.test", "admin", "secret")
    client._request = AsyncMock()

    assert await client.get_users([]) == []
    client._request.assert_not_called()


def test_build_inbounds_payload_keeps_only_protocols_with_tags():
    response = {
        "vless": [{"tag": "VLESS TCP REALITY"}, {"tag": "VLESS GRPC"}],
        "vmess": [],
        "trojan": [{"no_tag": 1}],
        "other": "not-a-list",
    }

    assert build_inbounds_payload(response) == {"vless": ["VLESS TCP REALITY", "VLESS GRPC"]}


# ---------- Telegram notifier ----------

class RecordingBot:
    def __init__(self, errors=None):
        self.errors = list(errors or [])
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(kwargs)


async def _no_sleep(_: float) -> None:
    return None


def _notifier(bot) -> TelegramTrialNotifier:
    return TelegramTrialNotifier(bot, send_delay=0, sleep=_no_sleep)


async def _send(notifier, kind=Kind.LOW):
    return await notifier.send(1, kind, used_bytes=8 * GB, limit_bytes=10 * GB, ended_reason="traffic")


async def test_notifier_sends_html_message_with_tariff_buttons():
    bot = RecordingBot()

    assert await _send(_notifier(bot)) == SendOutcome.SENT

    message = bot.sent[0]
    assert message["chat_id"] == 1 and message["parse_mode"] == "HTML"
    buttons = [b for row in message["reply_markup"].inline_keyboard for b in row]
    callbacks = [b.callback_data for b in buttons if b.callback_data]
    assert callbacks == ["tariff_50", "tariff_150", "tariff_450"]  # ведут в существующий сценарий оплаты
    assert any(b.url == "https://t.me/starArsonist" for b in buttons)


async def test_notifier_waits_and_retries_on_flood_control():
    bot = RecordingBot(errors=[RetryAfter(3)])
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    notifier = TelegramTrialNotifier(bot, send_delay=0, sleep=record_sleep)

    assert await _send(notifier) == SendOutcome.SENT
    assert len(bot.sent) == 1
    assert 4 in sleeps  # retry_after + 1


async def test_notifier_gives_up_after_repeated_flood_control():
    bot = RecordingBot(errors=[RetryAfter(1)] * 10)

    assert await _send(_notifier(bot)) == SendOutcome.FAILED


async def test_notifier_reports_blocked_bot():
    assert await _send(_notifier(RecordingBot([Forbidden("Forbidden: bot was blocked by the user")]))) == SendOutcome.BLOCKED
    assert await _send(_notifier(RecordingBot([BadRequest("Chat not found")]))) == SendOutcome.BLOCKED


async def test_notifier_reports_transient_errors_as_failed():
    assert await _send(_notifier(RecordingBot([NetworkError("boom")]))) == SendOutcome.FAILED
    assert await _send(_notifier(RecordingBot([BadRequest("Something else")]))) == SendOutcome.FAILED


def test_notification_texts_match_their_purpose():
    low = build_notification_text(Kind.LOW, 8 * GB, 10 * GB, None)
    assert "8.0 из 10.0 ГБ" in low and "почти закончился" in low

    ended_traffic = build_notification_text(Kind.ENDED, None, 10 * GB, "traffic")
    ended_time = build_notification_text(Kind.ENDED, None, 10 * GB, "time")
    assert "исчерпан трафик" in ended_traffic
    assert "истёк срок" in ended_time

    assert "Напоминание" in build_notification_text(Kind.REMINDER, None, 10 * GB, "time")
    assert len(tariff_keyboard().inline_keyboard) == 4  # 3 тарифа + поддержка


# ---------- Start screen / trial handler ----------

def _start_update(user_id: int = 7):
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="tester"), message=message, callback_query=None
    ), message


def _first_buttons(message) -> list[str]:
    markup = message.reply_text.call_args.kwargs["reply_markup"]
    return [row[0].callback_data or row[0].url for row in markup.inline_keyboard]


class StaticOffer:
    config = make_config()

    def __init__(self, value=True, error=False):
        self.value, self.error = value, error

    async def is_offered(self, telegram_id):
        if self.error:
            raise RuntimeError("db down")
        return self.value


async def test_start_screen_puts_trial_button_first_when_offered():
    update, message = _start_update()
    context = SimpleNamespace(bot_data={"trial_offer_uc": StaticOffer(True)})

    await start_handler(update, context)

    assert _first_buttons(message) == ["trial_start", "buy_vpn", "my_subscription", "https://t.me/starArsonist"]


@pytest.mark.parametrize(
    "bot_data",
    [{"trial_offer_uc": StaticOffer(False)}, {"trial_offer_uc": StaticOffer(error=True)}, {}],
)
async def test_start_screen_without_trial_button(bot_data):
    update, message = _start_update()

    await start_handler(update, SimpleNamespace(bot_data=bot_data))

    assert _first_buttons(message) == ["buy_vpn", "my_subscription", "https://t.me/starArsonist"]


async def test_repeated_start_never_issues_a_trial(env):
    offer = TrialOfferUseCase(env.repo, make_config())
    context = SimpleNamespace(bot_data={"trial_offer_uc": offer})

    for _ in range(3):
        update, message = _start_update(user_id=7)
        await start_handler(update, context)
        assert _first_buttons(message)[0] == "trial_start"  # только предложение, без выдачи

    assert await env.repo.get(7) is None
    assert env.marzban.calls == []


class StaticActivate:
    def __init__(self, result):
        self.result = result

    async def execute(self, telegram_id, username):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _callback_update():
    query = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
    return SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=7, username="tester")
    ), query


async def test_trial_handler_shows_link_and_instructions_on_success():
    update, query = _callback_update()
    from datetime import datetime

    result = TrialActivationResult(
        TrialResultKind.GRANTED,
        sub_url="https://sub.test/u?a=1&b=2",
        data_gb=10,
        expires_at=datetime(2026, 10, 13),
    )

    await trial_start_handler(update, SimpleNamespace(bot_data={"activate_trial_uc": StaticActivate(result)}))

    text = query.message.edit_text.call_args.args[0]
    assert "Пробный период активирован" in text
    assert "10 ГБ" in text and "13.10.2026" in text
    assert "https://sub.test/u?a=1&amp;b=2" in text  # экранирование HTML
    assert "Happ" in text


@pytest.mark.parametrize(
    "kind", [TrialResultKind.CAP_REACHED, TrialResultKind.DISABLED, TrialResultKind.NOT_ELIGIBLE]
)
async def test_unavailable_trial_offers_to_buy_a_package(kind):
    text, markup = render_trial_result(TrialActivationResult(kind))

    assert "временно недоступен" in text
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "buy_vpn" in callbacks


async def test_trial_handler_survives_unexpected_error():
    update, query = _callback_update()

    await trial_start_handler(
        update, SimpleNamespace(bot_data={"activate_trial_uc": StaticActivate(RuntimeError("boom"))})
    )

    text = query.message.edit_text.call_args.args[0]
    assert "Не удалось активировать" in text


# ---------- Settings ----------

_REQUIRED = dict(
    telegram_bot_token="t", telegram_admin_id=1, marzban_api_url="http://m", marzban_username="a", marzban_password="p"
)


def test_trial_settings_have_safe_defaults():
    settings = Settings(_env_file=None, **_REQUIRED)

    assert (settings.trial_enabled, settings.trial_data_gb, settings.trial_days, settings.trial_daily_cap) == (
        True, 10, 7, 50,
    )


def test_trial_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("TRIAL_ENABLED", "false")
    monkeypatch.setenv("TRIAL_DATA_GB", "5")
    monkeypatch.setenv("TRIAL_DAYS", "3")
    monkeypatch.setenv("TRIAL_DAILY_CAP", "7")

    settings = Settings(_env_file=None, **_REQUIRED)

    assert (settings.trial_enabled, settings.trial_data_gb, settings.trial_days, settings.trial_daily_cap) == (
        False, 5, 3, 7,
    )
