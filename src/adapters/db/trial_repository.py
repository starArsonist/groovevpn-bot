from datetime import datetime
from loguru import logger
from sqlalchemy import and_, exists, func, or_, select, update, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.domain.models import Order, Trial, TrialStatus, User, VPNProfile
from src.domain.trial_rules import TrialAlreadyExistsError, TrialNotificationKind

_NOTIFIED_COLUMN = {
    TrialNotificationKind.LOW: "notified_low_at",
    TrialNotificationKind.ENDED: "notified_ended_at",
    TrialNotificationKind.REMINDER: "notified_reminder_at",
}


class TrialRepository:
    """Репозиторий триалов.

    Принимает фабрику сессий, а не сессию: каждая операция работает в своей
    короткой транзакции. Это нужно, потому что фоновая проверка триалов идёт
    параллельно хендлерам, а общая сессия приложения для этого небезопасна.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, telegram_id: int) -> Trial | None:
        async with self._session_factory() as session:
            result = await session.execute(select(Trial).where(Trial.telegram_id == telegram_id))
            return result.scalars().first()

    async def get_sub_url(self, marzban_username: str) -> str | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(VPNProfile.sub_url).where(VPNProfile.marzban_username == marzban_username)
            )
            return result.scalars().first()

    async def reserve(
        self,
        telegram_id: int,
        username: str | None,
        marzban_username: str,
        data_limit_bytes: int,
        now: datetime,
    ) -> Trial:
        """Резервирует запись триала (и строку пользователя, если её ещё нет).

        Уникальность по telegram_id гарантирует БД: при гонке проигравший
        получает TrialAlreadyExistsError.
        """
        async with self._session_factory() as session:
            try:
                if await session.get(User, telegram_id) is None:
                    session.add(User(id=telegram_id, username=username))
                trial = Trial(
                    telegram_id=telegram_id,
                    marzban_username=marzban_username,
                    status=TrialStatus.RESERVED,
                    data_limit_bytes=data_limit_bytes,
                    created_at=now,
                )
                session.add(trial)
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise TrialAlreadyExistsError(f"Trial already exists for {telegram_id}") from exc
            return trial

    async def release(self, telegram_id: int) -> bool:
        """Откат резерва (только для не активированной записи)."""
        async with self._session_factory() as session:
            result = await session.execute(
                delete(Trial).where(
                    Trial.telegram_id == telegram_id, Trial.status == TrialStatus.RESERVED
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def activate(
        self,
        telegram_id: int,
        marzban_username: str,
        sub_url: str,
        granted_at: datetime,
        expires_at: datetime,
    ) -> Trial:
        """Атомарно: профиль VPN + статус триала active."""
        async with self._session_factory() as session:
            trial = (
                await session.execute(select(Trial).where(Trial.telegram_id == telegram_id))
            ).scalar_one()
            profile = (
                await session.execute(
                    select(VPNProfile).where(VPNProfile.marzban_username == marzban_username)
                )
            ).scalars().first()
            if profile is None:
                session.add(
                    VPNProfile(
                        user_id=telegram_id,
                        marzban_username=marzban_username,
                        sub_url=sub_url,
                        status="active",
                    )
                )
            else:
                profile.sub_url = sub_url
                profile.status = "active"
            trial.status = TrialStatus.ACTIVE
            trial.granted_at = granted_at
            trial.expires_at = expires_at
            await session.commit()
            return trial

    async def count_created_since(self, since: datetime) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count()).select_from(Trial).where(Trial.created_at >= since)
            )
            return int(result.scalar_one())

    async def has_paid_order(self, user_id: int) -> bool:
        return await self._order_exists(user_id, "completed")

    async def has_pending_order(self, user_id: int) -> bool:
        return await self._order_exists(user_id, "pending")

    async def _order_exists(self, user_id: int, status: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                select(exists().where(Order.user_id == user_id, Order.status == status))
            )
            return bool(result.scalar())

    async def paid_user_ids(self, user_ids: list[int]) -> set[int]:
        if not user_ids:
            return set()
        async with self._session_factory() as session:
            result = await session.execute(
                select(Order.user_id)
                .where(Order.user_id.in_(user_ids), Order.status == "completed")
                .distinct()
            )
            return set(result.scalars().all())

    async def has_unconverted(self, user_id: int) -> bool:
        """Есть триал, ещё не конвертированный в платный пакет (активный или закончившийся)."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(
                    exists().where(
                        Trial.telegram_id == user_id,
                        Trial.status.in_([TrialStatus.ACTIVE, TrialStatus.ENDED]),
                    )
                )
            )
            return bool(result.scalar())

    async def mark_converted(self, user_id: int, now: datetime) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                update(Trial)
                .where(Trial.telegram_id == user_id, Trial.status != TrialStatus.CONVERTED)
                .values(status=TrialStatus.CONVERTED, converted_at=now)
            )
            await session.commit()
            return bool(result.rowcount)

    async def list_monitored(self) -> list[Trial]:
        """Триалы, за которыми следит фоновая проверка.

        Активные - всегда (в том числе заблокировавшие бота: метки аналитики
        продолжают проставляться). Закончившиеся - пока возможно напоминание.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(Trial).where(
                    or_(
                        Trial.status == TrialStatus.ACTIVE,
                        and_(
                            Trial.status == TrialStatus.ENDED,
                            Trial.notified_reminder_at.is_(None),
                            Trial.bot_blocked_at.is_(None),
                        ),
                    )
                )
            )
            return list(result.scalars().all())

    async def mark_reached_low(self, trial_id: int, now: datetime) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                update(Trial)
                .where(Trial.id == trial_id, Trial.reached_80_at.is_(None))
                .values(reached_80_at=now)
            )
            await session.commit()
            return bool(result.rowcount)

    async def mark_ended(self, trial_id: int, reason: str, now: datetime) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                update(Trial)
                .where(Trial.id == trial_id, Trial.status == TrialStatus.ACTIVE)
                .values(status=TrialStatus.ENDED, ended_at=now, ended_reason=reason)
            )
            await session.commit()
            return bool(result.rowcount)

    async def claim_notification(
        self,
        trial_id: int,
        kind: TrialNotificationKind,
        now: datetime,
        reminder_cutoff: datetime,
    ) -> bool:
        """Атомарно захватывает флаг уведомления. True - можно отправлять."""
        conditions = [Trial.id == trial_id, Trial.bot_blocked_at.is_(None)]
        if kind == TrialNotificationKind.LOW:
            conditions += [
                Trial.status == TrialStatus.ACTIVE,
                Trial.reached_80_at.is_not(None),
                Trial.notified_low_at.is_(None),
            ]
        elif kind == TrialNotificationKind.ENDED:
            conditions += [Trial.status == TrialStatus.ENDED, Trial.notified_ended_at.is_(None)]
        else:
            conditions += [
                Trial.status == TrialStatus.ENDED,
                Trial.notified_reminder_at.is_(None),
                Trial.ended_at <= reminder_cutoff,
            ]
        async with self._session_factory() as session:
            result = await session.execute(
                update(Trial).where(*conditions).values({_NOTIFIED_COLUMN[kind]: now})
            )
            await session.commit()
            return result.rowcount == 1

    async def release_notification(self, trial_id: int, kind: TrialNotificationKind) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(Trial).where(Trial.id == trial_id).values({_NOTIFIED_COLUMN[kind]: None})
            )
            await session.commit()
        logger.info(f"Released notification flag {kind} for trial {trial_id}")

    async def mark_blocked(self, trial_id: int, now: datetime) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(Trial).where(Trial.id == trial_id).values(bot_blocked_at=now)
            )
            await session.commit()
