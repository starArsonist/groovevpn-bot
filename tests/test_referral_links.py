import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from src.domain.models import ReferralLink
from src.domain.referral_rules import ReferralConfig
from src.use_cases.referral_use_cases import BotUsernameProvider, OverviewStatus
from tests.referral_fakes import BOT_NAME, CaptureLogs, build_ref_env

INVITER = 10


async def _link_count(renv) -> int:
    async with renv.session_factory() as session:
        return (await session.execute(select(func.count()).select_from(ReferralLink))).scalar_one()


async def test_first_open_creates_one_link_with_expected_format(renv):
    overview = await renv.links.show(INVITER, "inviter")

    assert overview.status == OverviewStatus.OK
    assert len(overview.links) == 1
    url = overview.links[0].url
    assert url.startswith(f"https://t.me/{BOT_NAME}?start=ref_")
    param = url.split("?start=")[1]
    assert len(param) <= 64
    assert overview.can_create is True
    assert overview.invited == 0 and overview.balance == 0


async def test_opening_again_shows_the_same_link_instead_of_creating_another(renv):
    first = await renv.links.show(INVITER, "inviter")
    second = await renv.links.show(INVITER, "inviter")

    assert [l.url for l in first.links] == [l.url for l in second.links]
    assert await _link_count(renv) == 1


async def test_at_most_three_unused_links_and_existing_are_shown_on_overflow(renv):
    for _ in range(3):
        overview = await renv.links.create(INVITER, "inviter")
        assert overview.new_link is not None
    assert len(overview.links) == 3 and overview.can_create is False

    overflow = await renv.links.create(INVITER, "inviter")

    assert overflow.new_link is None
    assert [l.url for l in overflow.links] == [l.url for l in overview.links]
    assert await _link_count(renv) == 3


async def test_links_are_created_atomically_under_concurrency(renv):
    results = await asyncio.gather(*(renv.links.create(INVITER, "inviter") for _ in range(8)))

    assert sum(1 for r in results if r.new_link is not None) == 3
    assert await _link_count(renv) == 3


async def test_used_link_frees_a_slot(renv):
    for _ in range(3):
        await renv.links.create(INVITER, "inviter")
    token = (await renv.referrals.list_active_links(INVITER))[0].token
    assert (await renv.accept.execute(50, "friend", f"ref_{token}")).bound is True

    overview = await renv.links.create(INVITER, "inviter")

    assert overview.new_link is not None
    assert len(overview.links) == 3  # использованная ссылка в списке активных не показывается


async def test_revoked_link_frees_a_slot_and_is_no_longer_valid(renv):
    for _ in range(3):
        await renv.links.create(INVITER, "inviter")
    victim = (await renv.referrals.list_active_links(INVITER))[0]

    after_revoke = await renv.links.revoke(INVITER, victim.id)
    assert len(after_revoke.links) == 2 and after_revoke.can_create is True

    assert (await renv.accept.execute(50, "friend", f"ref_{victim.token}")).bound is False
    assert (await renv.links.create(INVITER, "inviter")).new_link is not None


async def test_cannot_revoke_foreign_or_used_link(renv):
    link = await renv.make_link(INVITER)

    await renv.links.revoke(77, link.id)  # чужая ссылка
    assert len(await renv.referrals.list_active_links(INVITER)) == 1

    assert (await renv.accept.execute(50, "friend", f"ref_{link.token}")).bound is True
    await renv.links.revoke(INVITER, link.id)  # уже использована
    async with renv.session_factory() as session:
        stored = await session.get(ReferralLink, link.id)
    assert stored.revoked_at is None and stored.used_at is not None


@pytest.mark.parametrize("days", [1, 30, 400, 3650])
async def test_link_has_no_expiry(renv, days):
    link = await renv.make_link(INVITER)

    renv.clock.advance(days=days)

    assert (await renv.accept.execute(50, "friend", f"ref_{link.token}")).bound is True


async def test_disabled_feature_creates_no_links(env, shared_session):
    renv = build_ref_env(env, shared_session, ReferralConfig(enabled=False))

    overview = await renv.links.show(INVITER, "inviter")

    assert overview.status == OverviewStatus.DISABLED
    assert await _link_count(renv) == 0
    assert (await renv.links.create(INVITER, "inviter")).status == OverviewStatus.DISABLED
    assert await _link_count(renv) == 0


async def test_overview_without_bot_username_is_unavailable_and_creates_nothing(renv):
    renv.username.username = None

    overview = await renv.links.show(INVITER, "inviter")

    assert overview.status == OverviewStatus.UNAVAILABLE
    assert await _link_count(renv) == 0


async def test_overview_shows_invited_count_and_balance(renv):
    await renv.bind(INVITER, 50)
    await renv.bind(INVITER, 51)
    await renv.add_balance(INVITER, 39)

    overview = await renv.links.show(INVITER, "inviter")

    assert overview.invited == 2
    assert overview.balance == 39


async def test_custom_max_active_links_is_respected(env, shared_session):
    renv = build_ref_env(env, shared_session, ReferralConfig(max_active_links=1))
    assert (await renv.links.create(INVITER, "inviter")).new_link is not None
    assert (await renv.links.create(INVITER, "inviter")).new_link is None


# ---------- имя бота ----------

class _Bot:
    def __init__(self, username: str | None, fail: bool = False) -> None:
        self.username = username
        self.fail = fail
        self.calls = 0

    async def get_me(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("network")
        return SimpleNamespace(username=self.username)


async def test_username_from_get_me_is_cached():
    bot = _Bot("real_bot")
    provider = BotUsernameProvider(bot, "")

    assert await provider.get() == "real_bot"
    assert await provider.get() == "real_bot"
    assert bot.calls == 1


async def test_configured_username_wins_and_mismatch_is_logged():
    bot = _Bot("real_bot")
    provider = BotUsernameProvider(bot, "@Configured_Bot")

    with CaptureLogs() as logs:
        assert await provider.get() == "Configured_Bot"
        assert await provider.get() == "Configured_Bot"

    assert "WARNING" in logs.text and "BOT_USERNAME" in logs.text
    assert logs.text.count("BOT_USERNAME") == 1  # предупреждаем один раз
    assert bot.calls == 1


async def test_matching_username_does_not_warn():
    provider = BotUsernameProvider(_Bot("Real_Bot"), "real_bot")

    with CaptureLogs() as logs:
        assert await provider.get() == "real_bot"

    assert "WARNING" not in logs.text


async def test_get_me_failure_falls_back_to_configured_or_none_without_caching_failure():
    assert await BotUsernameProvider(_Bot(None, fail=True), "cfg_bot").get() == "cfg_bot"

    bot = _Bot("later_bot", fail=True)
    provider = BotUsernameProvider(bot, "")
    assert await provider.get() is None
    bot.fail = False
    assert await provider.get() == "later_bot"
