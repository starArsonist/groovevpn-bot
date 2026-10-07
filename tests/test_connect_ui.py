from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from loguru import logger

from src.adapters.db.repositories import OrderRepository, UserRepository, VPNProfileRepository
from src.adapters.tg_bot.connect import CONNECT_HINT, MAX_BUTTON_URL_LENGTH, build_connect_keyboard
from src.adapters.tg_bot.handlers.subscription import my_subscription_handler
from src.adapters.tg_bot.handlers.trial import HAPP_HOWTO, render_trial_result, trial_start_handler
from src.use_cases.admin_use_cases import AdminUseCases
from src.use_cases.order_use_cases import CreateOrderUseCase
from src.use_cases.trial_use_cases import TrialActivationResult, TrialResultKind
from tests.fakes import GB, FakeBot

PAGE = "https://connect.example.com/c/"
APPS = "happ,v2raytun,hiddify"
SECRET = "SECRETTOKEN9f3a1c"
SUB_URL = f"https://sub.example.com:8443/sub/{SECRET}"
SUPPORT_URL = "https://t.me/starArsonist"


def _keyboard():
    return build_connect_keyboard(PAGE, APPS)


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def _connect_buttons(markup):
    return [b for b in _buttons(markup) if b.url and b.url.startswith(PAGE)]


def _labels(markup):
    return [b.text for b in _buttons(markup)]


def _decoded_sub(button) -> str:
    return parse_qs(urlsplit(button.url).fragment)["sub"][0]


class CaptureLogs:
    def __enter__(self):
        self.records: list[str] = []
        self._id = logger.add(lambda message: self.records.append(str(message)), level="DEBUG", format="{level} {message}")
        return self

    def __exit__(self, *exc):
        logger.remove(self._id)

    @property
    def text(self) -> str:
        return "\n".join(self.records)


# ---------- клавиатура ----------

def test_keyboard_has_one_button_per_app_in_configured_order():
    rows = build_connect_keyboard(PAGE, "hiddify,happ").app_rows(SUB_URL, "test")

    assert [row[0].text for row in rows] == ["Подключить в Hiddify", "Подключить в Happ"]
    assert all(len(row) == 1 for row in rows)
    assert [parse_qs(urlsplit(r[0].url).fragment)["app"][0] for r in rows] == ["hiddify", "happ"]
    assert all(_decoded_sub(r[0]) == SUB_URL for r in rows)


@pytest.mark.parametrize("page_url", ["", "http://connect.example.com/", "https://localhost/", "https://bad host.com/", None])
def test_invalid_or_empty_config_disables_buttons_and_does_not_crash(page_url):
    with CaptureLogs() as logs:
        keyboard = build_connect_keyboard(page_url, APPS)

    assert keyboard.enabled is False
    assert keyboard.app_rows(SUB_URL) == []
    assert "WARNING" in logs.text and "CONNECT_PAGE_URL" in logs.text


def test_empty_subscription_url_or_too_long_url_gives_no_buttons():
    keyboard = _keyboard()

    assert keyboard.app_rows("") == []
    assert keyboard.app_rows(None) == []
    with CaptureLogs() as logs:
        assert keyboard.app_rows("https://sub.example.com/" + "x" * MAX_BUTTON_URL_LENGTH) == []
    assert "too long" in logs.text


# ---------- триал ----------

def _callback_update(user_id: int = 7):
    query = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
    return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id, username="t")), query


async def _press_trial(env, keyboard, user_id: int = 7, bot_data_extra=None):
    update, query = _callback_update(user_id)
    bot_data = {"activate_trial_uc": env.activate, "connect_keyboard": keyboard, **(bot_data_extra or {})}
    await trial_start_handler(update, SimpleNamespace(bot_data=bot_data))
    return query.message.edit_text


async def test_trial_grant_shows_connect_buttons_under_the_link(env):
    env.marzban.sub_url = SUB_URL

    edit_text = await _press_trial(env, _keyboard())

    text = edit_text.call_args.args[0]
    markup = edit_text.call_args.kwargs["reply_markup"]
    assert f"<code>{SUB_URL}</code>" in text
    assert CONNECT_HINT in text and HAPP_HOWTO not in text
    assert _labels(markup) == [
        "Подключить в Happ", "Подключить в V2RayTun", "Подключить в Hiddify",
        "Моя подписка", "Купить VPN", "Поддержка",  # приложения -> действия -> «Поддержка»
    ]
    assert _buttons(markup)[-1].url == SUPPORT_URL
    assert all(_decoded_sub(b) == SUB_URL for b in _connect_buttons(markup))


async def test_repeated_trial_press_shows_the_link_with_buttons_again(env):
    env.marzban.sub_url = SUB_URL
    await _press_trial(env, _keyboard())

    edit_text = await _press_trial(env, _keyboard())

    assert "Пробный период уже выдан" in edit_text.call_args.args[0]
    assert len(_connect_buttons(edit_text.call_args.kwargs["reply_markup"])) == 3


async def test_trial_without_buttons_is_identical_to_previous_behavior(env):
    env.marzban.sub_url = SUB_URL

    edit_text = await _press_trial(env, build_connect_keyboard("", APPS))

    text = edit_text.call_args.args[0]
    markup = edit_text.call_args.kwargs["reply_markup"]
    expected_text, expected_markup = render_trial_result(
        TrialActivationResult(
            TrialResultKind.GRANTED, sub_url=SUB_URL, data_gb=10, expires_at=env.clock.now.replace(day=13)
        )
    )
    assert HAPP_HOWTO in text and CONNECT_HINT not in text
    assert _labels(markup) == _labels(expected_markup) == ["Моя подписка", "Купить VPN", "Поддержка"]
    assert "Пробный период активирован" in expected_text


async def test_trial_link_is_html_escaped(env):
    tricky = "https://sub.example.com/s?a=1&b=<2>"
    env.marzban.sub_url = tricky

    edit_text = await _press_trial(env, _keyboard())

    text = edit_text.call_args.args[0]
    assert "<code>https://sub.example.com/s?a=1&amp;b=&lt;2&gt;</code>" in text
    assert "b=<2>" not in text


async def test_trial_errors_and_unavailable_states_get_no_connect_buttons(env):
    env.marzban.fail["create_user"] = 1

    edit_text = await _press_trial(env, _keyboard())

    assert _connect_buttons(edit_text.call_args.kwargs["reply_markup"]) == []


async def test_telegram_rejecting_buttons_falls_back_to_text_without_losing_the_link(env):
    env.marzban.sub_url = SUB_URL
    update, query = _callback_update()
    attempts = []

    async def edit_text(text, reply_markup=None, parse_mode=None):
        attempts.append((text, reply_markup))
        if _connect_buttons(reply_markup):
            from telegram.error import BadRequest

            raise BadRequest(f"Button_url_invalid {PAGE}#app=happ&sub=...")

    query.message.edit_text = edit_text
    with CaptureLogs() as logs:
        await trial_start_handler(
            update, SimpleNamespace(bot_data={"activate_trial_uc": env.activate, "connect_keyboard": _keyboard()})
        )

    assert len(attempts) == 2
    final_text, final_markup = attempts[-1]
    assert f"<code>{SUB_URL}</code>" in final_text and HAPP_HOWTO in final_text  # прежняя инструкция
    assert _connect_buttons(final_markup) == []
    assert "resending without them" in logs.text
    assert PAGE not in logs.text and SECRET not in logs.text  # URL из текста ошибки вычищен


# ---------- первая покупка / продление ----------

async def _admin(session_factory, env, bot, keyboard):
    session = session_factory()
    shared = await session.__aenter__()
    admin = AdminUseCases(
        OrderRepository(shared), UserRepository(shared), VPNProfileRepository(shared),
        env.marzban, bot, trial_repo=env.repo, connect_keyboard=keyboard,
    )
    return admin, shared, session


async def _new_order(shared, user_id=222, tariff=50):
    uc = CreateOrderUseCase(OrderRepository(shared), UserRepository(shared), VPNProfileRepository(shared))
    return await uc.execute(user_id, "buyer", tariff, "photo")


async def test_first_purchase_shows_connect_buttons_then_support(env, session_factory):
    env.marzban.sub_url = SUB_URL
    bot = FakeBot()
    admin, shared, session = await _admin(session_factory, env, bot, _keyboard())
    try:
        order = await _new_order(shared)
        assert order.order_type == "new"

        assert await admin.approve_order(order.id) is True
    finally:
        await session.__aexit__(None, None, None)

    call = bot.calls[0]
    markup = call["reply_markup"]
    assert f"<code>{SUB_URL}</code>" in call["text"]
    assert CONNECT_HINT in call["text"] and "Happ:" not in call["text"]
    assert _labels(markup) == ["Подключить в Happ", "Подключить в V2RayTun", "Подключить в Hiddify", "Поддержка"]
    assert _buttons(markup)[-1].url == SUPPORT_URL


async def test_first_purchase_link_is_html_escaped(env, session_factory):
    env.marzban.sub_url = "https://sub.example.com/s?a=1&b=<2>"
    bot = FakeBot()
    admin, shared, session = await _admin(session_factory, env, bot, _keyboard())
    try:
        assert await admin.approve_order((await _new_order(shared)).id) is True
    finally:
        await session.__aexit__(None, None, None)

    assert "<code>https://sub.example.com/s?a=1&amp;b=&lt;2&gt;</code>" in bot.calls[0]["text"]


@pytest.mark.parametrize("keyboard_factory", [lambda: None, lambda: build_connect_keyboard("", APPS)])
async def test_first_purchase_without_buttons_is_unchanged(env, session_factory, keyboard_factory):
    env.marzban.sub_url = SUB_URL
    bot = FakeBot()
    admin, shared, session = await _admin(session_factory, env, bot, keyboard_factory())
    try:
        assert await admin.approve_order((await _new_order(shared)).id) is True
    finally:
        await session.__aexit__(None, None, None)

    call = bot.calls[0]
    assert call["reply_markup"] is None  # как раньше: сообщение без клавиатуры
    assert HAPP_HOWTO.split("\n")[0] in call["text"] and "Happ" in call["text"] and CONNECT_HINT not in call["text"]


async def test_first_purchase_telegram_rejection_still_delivers_the_link(env, session_factory):
    env.marzban.sub_url = SUB_URL
    bot = FakeBot(reject_if=lambda markup: bool(_connect_buttons(markup)))
    admin, shared, session = await _admin(session_factory, env, bot, _keyboard())
    try:
        assert await admin.approve_order((await _new_order(shared)).id) is True
    finally:
        await session.__aexit__(None, None, None)

    assert len(bot.calls) == 1  # сообщение со ссылкой доставлено, несмотря на отказ по кнопкам
    assert f"<code>{SUB_URL}</code>" in bot.calls[0]["text"]
    assert bot.calls[0]["reply_markup"] is None


async def test_fallback_to_new_user_after_marzban_404_is_a_first_issue_with_buttons(env, session_factory):
    await env.grant(111)
    del env.marzban.users["user_111_trial"]
    env.marzban.sub_url = SUB_URL
    bot = FakeBot()
    admin, shared, session = await _admin(session_factory, env, bot, _keyboard())
    try:
        order = await _new_order(shared, user_id=111)
        assert order.order_type == "topup"
        assert await admin.approve_order(order.id) is True
    finally:
        await session.__aexit__(None, None, None)

    assert len(_connect_buttons(bot.calls[0]["reply_markup"])) == 3  # выдана новая ссылка


async def test_renewal_has_no_connect_buttons(env, session_factory):
    env.marzban.sub_url = SUB_URL
    bot = FakeBot()
    admin, shared, session = await _admin(session_factory, env, bot, _keyboard())
    try:
        first = await _new_order(shared, user_id=333)
        assert await admin.approve_order(first.id) is True
        env.marzban.set_used("user_333_" + str(first.id), 10 * GB)

        renewal = await _new_order(shared, user_id=333)
        assert renewal.order_type == "topup"
        assert await admin.approve_order(renewal.id) is True
    finally:
        await session.__aexit__(None, None, None)

    first_call, renewal_call = bot.calls
    assert len(_connect_buttons(first_call["reply_markup"])) == 3
    assert renewal_call["reply_markup"] is None  # продление: без кнопок
    assert SUB_URL not in renewal_call["text"] and "Подключить" not in renewal_call["text"]


async def test_trial_user_purchase_is_a_renewal_without_connect_buttons(env, session_factory):
    await env.grant(111)
    bot = FakeBot()
    admin, shared, session = await _admin(session_factory, env, bot, _keyboard())
    try:
        order = await _new_order(shared, user_id=111)
        assert await admin.approve_order(order.id) is True
    finally:
        await session.__aexit__(None, None, None)

    assert bot.calls[0]["reply_markup"] is None
    assert "Подключить" not in bot.calls[0]["text"]


# ---------- «Моя подписка» ----------

class StubTraffic:
    def __init__(self, sub_url):
        self.sub_url = sub_url

    async def execute(self, user_id):
        return {
            "used_gb": 1.0, "limit_gb": 10.0, "remaining_gb": 9.0, "is_unlimited": False,
            "status": "active", "expire_at": None, "sub_url": self.sub_url,
        }


async def _open_subscription(sub_url, keyboard):
    update, query = _callback_update()
    context = SimpleNamespace(bot_data={"traffic_uc": StubTraffic(sub_url), "connect_keyboard": keyboard})
    await my_subscription_handler(update, context)
    return query.message.edit_text


async def test_subscription_screen_shows_connect_buttons_above_actions():
    edit_text = await _open_subscription(SUB_URL, _keyboard())

    markup = edit_text.call_args.kwargs["reply_markup"]
    assert f"<code>{SUB_URL}</code>" in edit_text.call_args.args[0]
    assert _labels(markup) == [
        "Подключить в Happ", "Подключить в V2RayTun", "Подключить в Hiddify",
        "Продлить / докупить", "🔙 Назад", "Поддержка",
    ]


async def test_subscription_screen_escapes_the_link():
    edit_text = await _open_subscription("https://sub.example.com/s?a=1&b=<2>", _keyboard())

    assert "<code>https://sub.example.com/s?a=1&amp;b=&lt;2&gt;</code>" in edit_text.call_args.args[0]


async def test_subscription_screen_without_config_is_as_before():
    edit_text = await _open_subscription(SUB_URL, build_connect_keyboard("", APPS))

    assert _labels(edit_text.call_args.kwargs["reply_markup"]) == ["Продлить / докупить", "🔙 Назад", "Поддержка"]


async def test_subscription_screen_without_profile_has_no_connect_buttons():
    update, query = _callback_update()

    class NoProfile:
        async def execute(self, user_id):
            return None

    await my_subscription_handler(
        update, SimpleNamespace(bot_data={"traffic_uc": NoProfile(), "connect_keyboard": _keyboard()})
    )

    assert _connect_buttons(query.message.edit_text.call_args.kwargs["reply_markup"]) == []


async def test_subscription_screen_falls_back_when_telegram_rejects_buttons():
    update, query = _callback_update()
    attempts = []

    async def edit_text(text, reply_markup=None, parse_mode=None):
        attempts.append(reply_markup)
        if _connect_buttons(reply_markup):
            from telegram.error import BadRequest

            raise BadRequest("Button_url_invalid")

    query.message.edit_text = edit_text
    await my_subscription_handler(
        update, SimpleNamespace(bot_data={"traffic_uc": StubTraffic(SUB_URL), "connect_keyboard": _keyboard()})
    )

    assert len(attempts) == 2 and _connect_buttons(attempts[-1]) == []


# ---------- логи ----------

async def test_subscription_url_and_page_url_never_reach_the_logs(env, session_factory):
    env.marzban.sub_url = SUB_URL
    keyboard = _keyboard()

    with CaptureLogs() as logs:
        await _press_trial(env, keyboard)  # выдача триала + кнопки
        await _press_trial(env, keyboard)  # повторный показ
        await _open_subscription(SUB_URL, keyboard)  # «Моя подписка»

        env.marzban.users.clear()
        bot = FakeBot()
        admin, shared, session = await _admin(session_factory, env, bot, keyboard)
        try:
            order = await _new_order(shared, user_id=444)
            assert await admin.approve_order(order.id) is True  # первая покупка

            rejecting = FakeBot(reject_if=lambda markup: bool(_connect_buttons(markup)))
            admin_rejecting, shared2, session2 = await _admin(session_factory, env, rejecting, keyboard)
            try:
                second = await _new_order(shared2, user_id=555)
                assert await admin_rejecting.approve_order(second.id) is True  # отказ Telegram + повтор
            finally:
                await session2.__aexit__(None, None, None)
        finally:
            await session.__aexit__(None, None, None)

        build_connect_keyboard("http://bad", APPS)  # предупреждение конфигурации

    assert "Connect buttons prepared" in logs.text  # факт показа кнопок в логе есть
    assert SECRET not in logs.text
    assert SUB_URL not in logs.text
    assert "sub.example.com" not in logs.text
    assert "connect.example.com" not in logs.text and "#app=" not in logs.text
