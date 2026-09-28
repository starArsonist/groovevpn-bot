from sqlalchemy import Integer, BigInteger, String, DateTime, Boolean, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.domain.base import Base

class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    tariff_gb: Mapped[int] = mapped_column(Integer)
    order_type: Mapped[str] = mapped_column(String, default="new")  # new, topup
    status: Mapped[str] = mapped_column(String, default="pending")  # pending, completed, rejected
    photo_file_id: Mapped[str] = mapped_column(String)
    created_at: Mapped[DateTime] = mapped_column(DateTime, default=func.now(), server_default=func.now())

    # Идемпотентность применения плана продления на Marzban: план считается один
    # раз, до первого мутирующего вызова панели, и сохраняется сюда. При повторной
    # обработке заказа (retry после сбоя, повторный вебхук) он не пересчитывается.
    plan_computed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    planned_data_limit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # None = не менять лимит (безлимит)
    planned_expire_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    carried_over_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reset_applied: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    user = relationship("User", backref="orders")
