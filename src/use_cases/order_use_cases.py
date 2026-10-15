from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from loguru import logger

from src.adapters.db.order_settlement import BALANCE_PHOTO, OrderSettlement
from src.adapters.db.repositories import OrderRepository, UserRepository, VPNProfileRepository
from src.domain.clock import utc_now
from src.domain.models import Order, OrderPayment
from src.domain.tariffs import get_tariff


class UnknownTariffError(Exception):
    """Тариф с таким размером пакета не существует: заказ без реальной цены не создаётся."""


@dataclass(frozen=True)
class PlacedOrder:
    order: Order
    payment: OrderPayment | None  # None - заказ создан без учёта баланса (фича оплаты не подключена)


class CreateOrderUseCase:
    def __init__(
        self,
        order_repo: OrderRepository,
        user_repo: UserRepository,
        vpn_repo: VPNProfileRepository,
        settlement: OrderSettlement | None = None,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.order_repo = order_repo
        self.user_repo = user_repo
        self.vpn_repo = vpn_repo
        self.settlement = settlement
        self._clock = clock

    async def execute(self, user_id: int, username: str | None, tariff_gb: int, photo_file_id: str) -> Order:
        return (await self.place(user_id, username, tariff_gb, photo_file_id)).order

    async def place(
        self,
        user_id: int,
        username: str | None,
        tariff_gb: int,
        photo_file_id: str,
        balance_rub: int = 0,
        require_full: bool = False,
    ) -> PlacedOrder:
        """Создаёт заказ. С подключённым `OrderSettlement` вместе с записью об оплате и удержанием баланса
        (`balance_rub` - сумма, показанная пользователю на экране оплаты)."""
        logger.info(f"Creating order for user {user_id}, tariff {tariff_gb}GB")

        # Ensure user exists
        await self.user_repo.get_or_create(user_id=user_id, username=username)

        # Check if user already has a VPN profile
        profile = await self.vpn_repo.get_by_user_id(user_id)
        order_type = "topup" if profile else "new"

        if self.settlement is None:
            order = await self.order_repo.create(
                user_id=user_id,
                tariff_gb=tariff_gb,
                order_type=order_type,
                status="pending",
                photo_file_id=photo_file_id
            )
            logger.info(f"Order created with ID {order.id}, type: {order_type}")
            return PlacedOrder(order=order, payment=None)

        tariff = get_tariff(tariff_gb)
        if tariff is None or tariff.price_rub <= 0:
            logger.warning(f"Order for user {user_id} refused: no price for tariff {tariff_gb}GB")
            raise UnknownTariffError(str(tariff_gb))

        placed = await self.settlement.place_order(
            user_id=user_id,
            username=username,
            tariff_gb=tariff_gb,
            order_type=order_type,
            photo_file_id=BALANCE_PHOTO if require_full else photo_file_id,
            price_rub=tariff.price_rub,
            balance_limit_rub=balance_rub,
            now=self._clock(),
            require_full=require_full,
        )
        logger.info(
            f"Order created with ID {placed.order.id}, type: {order_type}, "
            f"price {placed.payment.price_rub}, balance {placed.payment.balance_rub}, cash {placed.payment.cash_rub}"
        )
        return PlacedOrder(order=placed.order, payment=placed.payment)
