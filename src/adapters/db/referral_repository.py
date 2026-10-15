from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import DateTime, exists, func, insert, literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.adapters.db.transactions import run_in_transaction
from src.domain.models import Order, OrderPayment, Referral, ReferralLink, ReferralStatus, User


@dataclass(frozen=True)
class BindOutcome:
    link_id: int
    referral_id: int
    inviter_id: int


async def ensure_user(session: AsyncSession, user_id: int, username: str | None) -> None:
    if await session.get(User, user_id) is None:
        session.add(User(id=user_id, username=username))
        await session.flush()


async def user_has_cash_paid_order(session: AsyncSession, user_id: int) -> bool:
    """Есть ли подтверждённый заказ с оплатой деньгами: право приглашать.

    Заказ без записи об оплате создан до релиза фичи и оплачен деньгами; заказ, оплаченный
    балансом целиком (cash_rub = 0), не считается. Право монотонно: подтверждённый заказ не откатывается.
    """
    result = await session.execute(
        select(Order.id)
        .outerjoin(OrderPayment, OrderPayment.order_id == Order.id)
        .where(
            Order.user_id == user_id,
            Order.status == "completed",
            or_(OrderPayment.order_id.is_(None), OrderPayment.cash_rub > 0),
        )
        .limit(1)
    )
    return result.first() is not None


class ReferralRepository:
    """Ссылки, привязки и флаги уведомлений. Каждая операция - короткая транзакция на своей сессии;
    состязательные условия (лимит ссылок, «сжигание», заявка на бонус) выражены условными SQL-операторами."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def has_cash_paid_order(self, user_id: int) -> bool:
        async with self._session_factory() as session:
            return await user_has_cash_paid_order(session, user_id)

    # ---------- ссылки ----------

    async def create_link(self, inviter_id: int, username: str | None, token: str, now: datetime, max_active: int) -> ReferralLink | None:
        """Создаёт ссылку, если неиспользованных меньше `max_active` (проверка и вставка - один оператор)."""

        async def work(session: AsyncSession) -> ReferralLink | None:
            await ensure_user(session, inviter_id, username)
            active = (
                select(func.count())
                .select_from(ReferralLink)
                .where(
                    ReferralLink.inviter_id == inviter_id,
                    ReferralLink.used_at.is_(None),
                    ReferralLink.revoked_at.is_(None),
                )
                .scalar_subquery()
            )
            result = await session.execute(
                insert(ReferralLink).from_select(
                    ["inviter_id", "token", "created_at"],
                    select(literal(inviter_id), literal(token), literal(now, DateTime())).where(active < max_active),
                )
            )
            if result.rowcount != 1:
                return None
            return (await session.execute(select(ReferralLink).where(ReferralLink.token == token))).scalar_one()

        return await run_in_transaction(self._session_factory, work)

    async def list_active_links(self, inviter_id: int) -> list[ReferralLink]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ReferralLink)
                .where(
                    ReferralLink.inviter_id == inviter_id,
                    ReferralLink.used_at.is_(None),
                    ReferralLink.revoked_at.is_(None),
                )
                .order_by(ReferralLink.id)
            )
            return list(result.scalars().all())

    async def revoke_link(self, inviter_id: int, link_id: int, now: datetime) -> bool:
        async def work(session: AsyncSession) -> bool:
            result = await session.execute(
                update(ReferralLink)
                .where(
                    ReferralLink.id == link_id,
                    ReferralLink.inviter_id == inviter_id,
                    ReferralLink.used_at.is_(None),
                    ReferralLink.revoked_at.is_(None),
                )
                .values(revoked_at=now)
                .execution_options(synchronize_session=False)
            )
            return result.rowcount == 1

        return await run_in_transaction(self._session_factory, work)

    async def count_invited(self, inviter_id: int) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count()).select_from(Referral).where(Referral.inviter_id == inviter_id)
            )
            return int(result.scalar_one())

    # ---------- привязка ----------

    async def bind(self, token: str, invitee_id: int, username: str | None, now: datetime) -> BindOutcome | None:
        """Атомарно «сжигает» ссылку и создаёт привязку; при любом отказе ссылка остаётся нетронутой.

        Условия в одном операторе: ссылка не использована и не отозвана, не принадлежит
        самому приглашённому, у него нет подтверждённого платного заказа. Уникальность
        `invitee_id` не даёт привязаться второй раз: тогда откатывается и «сжигание».
        """

        async def work(session: AsyncSession) -> BindOutcome | None:
            paid = exists().where(Order.user_id == invitee_id, Order.status == "completed")
            burned = await session.execute(
                update(ReferralLink)
                .where(
                    ReferralLink.token == token,
                    ReferralLink.used_at.is_(None),
                    ReferralLink.revoked_at.is_(None),
                    ReferralLink.inviter_id != invitee_id,
                    ~paid,
                )
                .values(used_at=now, invitee_id=invitee_id)
                .returning(ReferralLink.id, ReferralLink.inviter_id)
                .execution_options(synchronize_session=False)
            )
            row = burned.first()
            if row is None:
                return None
            await ensure_user(session, invitee_id, username)
            referral = Referral(
                link_id=row.id,
                inviter_id=row.inviter_id,
                invitee_id=invitee_id,
                status=ReferralStatus.BOUND,
                bound_at=now,
            )
            session.add(referral)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()  # приглашённый уже привязан: ссылка не сгорает
                return None
            return BindOutcome(link_id=row.id, referral_id=referral.id, inviter_id=row.inviter_id)

        return await run_in_transaction(self._session_factory, work)

    async def get_by_invitee(self, invitee_id: int) -> Referral | None:
        async with self._session_factory() as session:
            result = await session.execute(select(Referral).where(Referral.invitee_id == invitee_id))
            return result.scalars().first()

    async def get(self, referral_id: int) -> Referral | None:
        async with self._session_factory() as session:
            return await session.get(Referral, referral_id)

    # ---------- уведомления о награде ----------

    async def claim_reward_notification(self, referral_id: int, now: datetime) -> bool:
        """Атомарно захватывает право отправить уведомление (как флаги уведомлений триала)."""

        async def work(session: AsyncSession) -> bool:
            result = await session.execute(
                update(Referral)
                .where(
                    Referral.id == referral_id,
                    Referral.status == ReferralStatus.REWARDED,
                    Referral.notified_at.is_(None),
                    Referral.notify_blocked_at.is_(None),
                )
                .values(notified_at=now)
                .execution_options(synchronize_session=False)
            )
            return result.rowcount == 1

        return await run_in_transaction(self._session_factory, work)

    async def release_reward_notification(self, referral_id: int) -> None:
        async def work(session: AsyncSession) -> None:
            await session.execute(
                update(Referral)
                .where(Referral.id == referral_id, Referral.notify_blocked_at.is_(None))
                .values(notified_at=None)
                .execution_options(synchronize_session=False)
            )

        await run_in_transaction(self._session_factory, work)

    async def mark_notify_blocked(self, referral_id: int, now: datetime) -> None:
        async def work(session: AsyncSession) -> None:
            await session.execute(
                update(Referral)
                .where(Referral.id == referral_id)
                .values(notify_blocked_at=now)
                .execution_options(synchronize_session=False)
            )

        await run_in_transaction(self._session_factory, work)

    async def list_unnotified_rewards(self, limit: int = 50) -> list[Referral]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(Referral)
                .where(
                    Referral.status == ReferralStatus.REWARDED,
                    Referral.notified_at.is_(None),
                    Referral.notify_blocked_at.is_(None),
                )
                .order_by(Referral.id)
                .limit(limit)
            )
            return list(result.scalars().all())
