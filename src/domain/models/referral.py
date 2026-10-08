from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.domain.base import Base
from src.domain.clock import utc_now


class ReferralStatus:
    BOUND = "bound"
    REWARDED = "rewarded"
    CLOSED = "closed"


class CloseReason:
    NO_CASH = "no_cash"  # первый заказ оплачен балансом целиком
    MONTHLY_CAP = "monthly_cap"
    NO_REWARD = "no_reward"  # награда округлилась до нуля
    NOT_ELIGIBLE = "not_eligible"  # первый заказ без заявки (фича была выключена)
    INVITER_NOT_PAID = "inviter_not_paid"  # у пригласившего нет подтверждённого заказа с оплатой деньгами


class BalanceKind:
    REFERRAL_REWARD = "referral_reward"
    ORDER_SPEND = "order_spend"
    ORDER_REFUND = "order_refund"


class ReferralLink(Base):
    """Одноразовая бессрочная ссылка пригласившего."""

    __tablename__ = "referral_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    inviter_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    token: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invitee_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)


class Referral(Base):
    """Привязка приглашённого к пригласившему: один раз навсегда."""

    __tablename__ = "referrals"
    __table_args__ = (
        CheckConstraint("inviter_id <> invitee_id", name="ck_referrals_not_self"),
        Index("ix_referrals_inviter_status_rewarded", "inviter_id", "status", "rewarded_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    link_id: Mapped[int] = mapped_column(Integer, ForeignKey("referral_links.id"), unique=True)
    inviter_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    invitee_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    status: Mapped[str] = mapped_column(String, default=ReferralStatus.BOUND)
    close_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    bound_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    bonus_order_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("orders.id"), nullable=True)
    reward_rub: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rewarded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notify_blocked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BalanceEntry(Base):
    """Append-only журнал бонусного баланса; баланс = сумма записей."""

    __tablename__ = "balance_entries"
    __table_args__ = (
        UniqueConstraint("kind", "ref_id", name="uq_balance_entries_kind_ref"),
        CheckConstraint("amount_rub <> 0", name="ck_balance_entries_nonzero"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    amount_rub: Mapped[int] = mapped_column(Integer)
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class OrderPayment(Base):
    """Финансовые параметры заказа, зафиксированные при создании (1:1 с orders)."""

    __tablename__ = "order_payments"
    __table_args__ = (
        CheckConstraint(
            "balance_rub >= 0 AND cash_rub >= 0 AND balance_rub + cash_rub = price_rub",
            name="ck_order_payments_amounts",
        ),
    )

    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"), primary_key=True)
    price_rub: Mapped[int] = mapped_column(Integer)
    balance_rub: Mapped[int] = mapped_column(Integer, default=0)
    cash_rub: Mapped[int] = mapped_column(Integer)
    bonus_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
