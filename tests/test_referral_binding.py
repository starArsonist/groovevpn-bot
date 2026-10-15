import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.domain.models import Referral, ReferralLink, ReferralStatus
from src.domain.referral_rules import ReferralConfig
from tests.referral_fakes import build_ref_env

INVITER = 10
FRIEND = 50


async def _link(renv, token: str) -> ReferralLink:
    async with renv.session_factory() as session:
        return (await session.execute(select(ReferralLink).where(ReferralLink.token == token))).scalar_one()


async def _referral_count(renv) -> int:
    async with renv.session_factory() as session:
        return (await session.execute(select(func.count()).select_from(Referral))).scalar_one()


async def test_successful_binding_burns_the_link(renv):
    link = await renv.make_link(INVITER)

    result = await renv.accept.execute(FRIEND, "friend", f"ref_{link.token}")

    assert result.bound is True
    referral = await renv.referral_of(FRIEND)
    assert (referral.inviter_id, referral.status, referral.link_id) == (INVITER, ReferralStatus.BOUND, link.id)
    stored = await _link(renv, link.token)
    assert stored.used_at == renv.clock() and stored.invitee_id == FRIEND


async def test_user_with_trial_can_be_bound(renv):
    await renv.base.grant(FRIEND)
    link = await renv.make_link(INVITER)

    assert (await renv.accept.execute(FRIEND, "friend", f"ref_{link.token}")).bound is True


async def test_user_with_only_pending_order_can_be_bound(renv):
    await renv.base.add_order(FRIEND, "pending")
    link = await renv.make_link(INVITER)

    assert (await renv.accept.execute(FRIEND, "friend", f"ref_{link.token}")).bound is True


async def test_used_link_cannot_be_used_again(renv):
    link = await renv.make_link(INVITER)
    assert (await renv.accept.execute(FRIEND, "friend", f"ref_{link.token}")).bound is True

    assert (await renv.accept.execute(51, "second", f"ref_{link.token}")).bound is False

    assert await renv.referral_of(51) is None
    assert await _referral_count(renv) == 1


@pytest.mark.parametrize(
    "arg", [None, "", "ref_", "ref_x", "ref_" + "A" * 22, "start", "ref_" + "A" * 80, "ref_а" * 8]
)
async def test_garbage_or_unknown_start_parameter_does_not_bind(renv, arg):
    assert (await renv.accept.execute(FRIEND, "friend", arg)).bound is False
    assert await _referral_count(renv) == 0


async def test_own_link_is_rejected_and_stays_usable(renv):
    link = await renv.make_link(INVITER)

    assert (await renv.accept.execute(INVITER, "inviter", f"ref_{link.token}")).bound is False
    assert (await _link(renv, link.token)).used_at is None

    assert (await renv.accept.execute(FRIEND, "friend", f"ref_{link.token}")).bound is True


async def test_rebinding_is_impossible_and_second_link_is_not_burned(renv):
    first = await renv.make_link(INVITER)
    other_inviter_link = await renv.make_link(11)
    assert (await renv.accept.execute(FRIEND, "friend", f"ref_{first.token}")).bound is True

    assert (await renv.accept.execute(FRIEND, "friend", f"ref_{other_inviter_link.token}")).bound is False

    assert (await renv.referral_of(FRIEND)).inviter_id == INVITER
    assert (await _link(renv, other_inviter_link.token)).used_at is None
    assert (await renv.accept.execute(52, "third", f"ref_{other_inviter_link.token}")).bound is True


async def test_user_with_confirmed_paid_order_is_not_bound_and_link_survives(renv):
    await renv.base.add_order(FRIEND, "completed")
    link = await renv.make_link(INVITER)

    assert (await renv.accept.execute(FRIEND, "friend", f"ref_{link.token}")).bound is False

    assert (await _link(renv, link.token)).used_at is None
    assert await renv.referral_of(FRIEND) is None


async def test_two_people_opening_one_link_concurrently_bind_exactly_one(renv):
    link = await renv.make_link(INVITER)

    results = await asyncio.gather(
        *(renv.accept.execute(100 + i, f"friend{i}", f"ref_{link.token}") for i in range(6))
    )

    assert sum(1 for r in results if r.bound) == 1
    assert await _referral_count(renv) == 1
    assert (await _link(renv, link.token)).invitee_id is not None


async def test_one_person_opening_two_links_concurrently_binds_once_and_keeps_other_link(renv):
    a = await renv.make_link(INVITER)
    b = await renv.make_link(INVITER)

    results = await asyncio.gather(
        renv.accept.execute(FRIEND, "friend", f"ref_{a.token}"),
        renv.accept.execute(FRIEND, "friend", f"ref_{b.token}"),
    )

    assert sum(1 for r in results if r.bound) == 1
    assert await _referral_count(renv) == 1
    used = [(await _link(renv, t)).used_at is not None for t in (a.token, b.token)]
    assert sorted(used) == [False, True]


async def test_revoked_link_does_not_bind(renv):
    link = await renv.make_link(INVITER)
    await renv.links.revoke(INVITER, link.id)

    assert (await renv.accept.execute(FRIEND, "friend", f"ref_{link.token}")).bound is False
    assert await renv.referral_of(FRIEND) is None


async def test_disabled_feature_does_not_bind(env, shared_session):
    renv = build_ref_env(env, shared_session)
    link = await renv.make_link(INVITER)
    disabled = build_ref_env(env, shared_session, ReferralConfig(enabled=False))

    assert (await disabled.accept.execute(FRIEND, "friend", f"ref_{link.token}")).bound is False
    assert (await _link(renv, link.token)).used_at is None


async def test_database_forbids_self_binding_and_double_binding(renv):
    link = await renv.make_link(INVITER)
    other = await renv.make_link(INVITER)
    assert (await renv.accept.execute(FRIEND, "friend", f"ref_{link.token}")).bound is True

    async with renv.session_factory() as session:
        session.add(Referral(link_id=other.id, inviter_id=INVITER, invitee_id=FRIEND))  # invitee уже привязан
        with pytest.raises(IntegrityError):
            await session.commit()
    async with renv.session_factory() as session:
        session.add(Referral(link_id=other.id, inviter_id=INVITER, invitee_id=INVITER))  # сам к себе
        with pytest.raises(IntegrityError):
            await session.commit()
