from src.adapters.db.repositories import OrderRepository, UserRepository
from src.domain.models import Order, User
from loguru import logger

class CreateOrderUseCase:
    def __init__(self, order_repo: OrderRepository, user_repo: UserRepository):
        self.order_repo = order_repo
        self.user_repo = user_repo

    async def execute(self, user_id: int, username: str | None, tariff_gb: int, photo_file_id: str) -> Order:
        logger.info(f"Creating order for user {user_id}, tariff {tariff_gb}GB")
        
        # Ensure user exists
        await self.user_repo.get_or_create(user_id=user_id, username=username)
        
        # Create pending order
        order = await self.order_repo.create(
            user_id=user_id,
            tariff_gb=tariff_gb,
            status="pending",
            photo_file_id=photo_file_id
        )
        
        logger.info(f"Order created with ID {order.id}")
        return order
