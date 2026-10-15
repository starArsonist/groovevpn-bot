from datetime import datetime
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from src.domain.base import Base
from src.domain.clock import utc_now


class TrialStatus:
    RESERVED = "reserved"
    ACTIVE = "active"
    ENDED = "ended"
    CONVERTED = "converted"


class Trial(Base):
    """Один пробный период на один Telegram-аккаунт навсегда."""

    __tablename__ = "trials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    marzban_username: Mapped[str] = mapped_column(String, unique=True)
    status: Mapped[str] = mapped_column(String, default=TrialStatus.RESERVED, index=True)
    data_limit_bytes: Mapped[int] = mapped_column(BigInteger)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reached_80_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_reason: Mapped[str | None] = mapped_column(String, nullable=True)  # traffic / time
    converted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    notified_low_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notified_ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notified_reminder_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bot_blocked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
