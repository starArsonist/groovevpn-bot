from sqlalchemy import Integer, BigInteger, String, DateTime, ForeignKey, func
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

    user = relationship("User", backref="orders")
