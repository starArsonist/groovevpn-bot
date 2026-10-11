import httpx
import pytest

import src.adapters.marzban.client as client_module
from src.adapters.db.repositories import OrderRepository, UserRepository, VPNProfileRepository
from src.adapters.marzban.client import DEFAULT_TIMEOUT, MarzbanClient
from src.use_cases.admin_use_cases import AdminUseCases
from src.use_cases.order_use_cases import CreateOrderUseCase
from tests.fakes import FakeBot
from tests.referral_fakes import CaptureLogs

HTTPX_DEFAULT_SECONDS = 5.0


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _SpyAsyncClient:
    """Подмена httpx.AsyncClient: запоминает аргументы создания, сеть не использует."""

    created: list[dict] = []

    def __init__(self, **kwargs) -> None:
        type(self).created.append(kwargs)

    async def __aenter__(self) -> "_SpyAsyncClient":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def post(self, url, **kwargs) -> _Response:
        return _Response({"access_token": "token"})

    async def request(self, method, url, **kwargs) -> _Response:
        return _Response({"username": "u"})


@pytest.fixture
def spy(monkeypatch):
    _SpyAsyncClient.created = []
    monkeypatch.setattr(client_module.httpx, "AsyncClient", _SpyAsyncClient)
    return _SpyAsyncClient


def test_default_timeout_is_longer_than_the_httpx_default_of_5_seconds():
    assert DEFAULT_TIMEOUT.read > HTTPX_DEFAULT_SECONDS
    assert DEFAULT_TIMEOUT.connect > HTTPX_DEFAULT_SECONDS
    assert DEFAULT_TIMEOUT.write > HTTPX_DEFAULT_SECONDS
    assert DEFAULT_TIMEOUT.pool > HTTPX_DEFAULT_SECONDS


async def test_token_request_and_api_requests_use_the_explicit_timeout(spy):
    client = MarzbanClient("http://marzban.test", "admin", "secret")

    await client.get_user("someone")  # сначала запрашивается токен, затем сам запрос

    assert len(spy.created) == 2
    for kwargs in spy.created:
        assert kwargs["timeout"] is DEFAULT_TIMEOUT


async def test_timeout_can_be_overridden_per_client(spy):
    custom = httpx.Timeout(7.0, connect=3.0)
    client = MarzbanClient("http://marzban.test", "admin", "secret", timeout=custom)

    await client.get_user("someone")

    assert all(kwargs["timeout"] is custom for kwargs in spy.created)


# ---------- в логах видно тип исключения ----------

@pytest.fixture
async def shared_session(session_factory):
    async with session_factory() as session:
        yield session


async def test_failed_approval_logs_the_exception_type_even_when_its_message_is_empty(env, shared_session):
    uc = CreateOrderUseCase(
        OrderRepository(shared_session), UserRepository(shared_session), VPNProfileRepository(shared_session)
    )
    order = await uc.execute(111, "tester", 50, "photo")

    async def timing_out_inbounds():
        raise httpx.ConnectTimeout("")  # у таймаутов httpx текст ошибки пустой

    env.marzban.get_inbounds = timing_out_inbounds
    admin = AdminUseCases(
        OrderRepository(shared_session), UserRepository(shared_session), VPNProfileRepository(shared_session),
        env.marzban, FakeBot(), trial_repo=env.repo,
    )

    with CaptureLogs() as logs:
        assert await admin.approve_order(order.id) is False

    line = next(r for r in logs.records if "Error approving order" in r)
    assert f"Error approving order {order.id}: ConnectTimeout" in line


async def test_trial_monitor_logs_the_exception_type_when_marzban_is_unreachable(env):
    await env.grant(1)

    async def timing_out_users(usernames):
        raise httpx.ReadTimeout("")

    env.marzban.get_users = timing_out_users

    with CaptureLogs() as logs:
        await env.monitor.run_once()

    line = next(r for r in logs.records if "Failed to fetch trial usage from Marzban" in r)
    assert "ReadTimeout" in line
