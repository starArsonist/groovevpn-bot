import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy.exc import IntegrityError, OperationalError

from src.adapters.db.referral_repository import ReferralRepository
from src.adapters.tg_bot.handlers.referral import REF_MENU, referral_menu_handler
from src.adapters.tg_bot.handlers.start import start_handler
from tests.referral_fakes import BOT_NAME, CaptureLogs

INVITER = 10
FRIEND = 50


@pytest.fixture(autouse=True)
async def _inviter_can_invite(renv):
    """Пригласивший уже платил деньгами (право приглашать); отдельные тесты проверяют и обратное."""
    await renv.make_eligible(INVITER)



def _assert_no_secrets(logs: CaptureLogs, *tokens: str) -> None:
    text = logs.text
    for token in tokens:
        assert token not in text
    assert "start=ref_" not in text
    assert f"t.me/{BOT_NAME}" not in text


async def test_full_referral_flow_never_logs_tokens_or_links(renv):
    with CaptureLogs() as logs:
        link = await renv.make_link(INVITER)
        spare = await renv.make_link(INVITER)
        await renv.links.show(INVITER, "inviter")
        assert (await renv.accept.execute(FRIEND, "friend", f"ref_{link.token}")).bound is True
        assert (await renv.accept.execute(51, "late", f"ref_{link.token}")).bound is False  # использована
        assert (await renv.accept.execute(INVITER, "inviter", f"ref_{spare.token}")).bound is False  # своя
        await renv.accept.execute(52, "junk", "ref_not-a-real-token-1234")
        await renv.links.revoke(INVITER, spare.id)
        placed = await renv.place(FRIEND, 50)
        await renv.admin().approve_order(placed.order.id)
        await renv.reward_notifier.run_once()

    _assert_no_secrets(logs, link.token, spare.token, "not-a-real-token-1234")
    # при этом факты логируются
    assert f"User {FRIEND} bound to inviter {INVITER}" in logs.text
    assert "Referral link" in logs.text and "reward" in logs.text.lower()


async def test_database_failures_in_referral_code_do_not_leak_the_token(renv, monkeypatch):
    link = await renv.make_link(INVITER)
    secret = link.token

    async def failing_bind(self, token, invitee_id, username, now):
        raise IntegrityError("INSERT INTO referrals ...", (token, invitee_id), Exception("constraint failed"))

    async def failing_create(self, inviter_id, username, token, now, max_active):
        raise OperationalError("INSERT INTO referral_links ...", (token,), Exception("database is locked"))

    monkeypatch.setattr(ReferralRepository, "bind", failing_bind)
    monkeypatch.setattr(ReferralRepository, "create_link", failing_create)

    with CaptureLogs() as logs:
        assert (await renv.accept.execute(FRIEND, "friend", f"ref_{secret}")).bound is False
        overview = await renv.links.create(INVITER, "inviter")
        assert overview.new_link is None

    assert "IntegrityError" in logs.text or "OperationalError" in logs.text
    _assert_no_secrets(logs, secret)
    assert secret not in repr(logs.records)


async def test_handlers_do_not_log_the_start_argument(renv):
    link = await renv.make_link(INVITER)
    reply = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=FRIEND, username="friend"),
        message=SimpleNamespace(reply_text=reply),
        callback_query=None,
    )
    context = SimpleNamespace(
        bot_data={"accept_referral_uc": renv.accept, "referral_config": renv.config},
        args=[f"ref_{link.token}"],
    )

    with CaptureLogs() as logs:
        await start_handler(update, context)

    _assert_no_secrets(logs, link.token)


async def test_invite_screen_logs_do_not_contain_links(renv):
    query = SimpleNamespace(
        data=REF_MENU, answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock())
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=INVITER, username="inviter"), callback_query=query)
    context = SimpleNamespace(bot_data={"referral_link_uc": renv.links, "referral_config": renv.config})

    with CaptureLogs() as logs:
        await referral_menu_handler(update, context)

    links = await renv.referrals.list_active_links(INVITER)
    assert links
    _assert_no_secrets(logs, *(link.token for link in links))
