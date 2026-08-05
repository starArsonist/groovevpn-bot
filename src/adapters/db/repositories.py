from typing import Generic, TypeVar, Type, Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update
from src.domain.base import Base
from src.domain.models import User, Order, VPNProfile

ModelType = TypeVar("ModelType", bound=Base)

class BaseRepository(Generic[ModelType]):
    def __init__(self, session: AsyncSession, model: Type[ModelType]):
        self.session = session
        self.model = model

    async def get_by_id(self, id: int) -> Optional[ModelType]:
        result = await self.session.execute(select(self.model).filter(self.model.id == id))
        return result.scalars().first()

    async def create(self, **kwargs) -> ModelType:
        obj = self.model(**kwargs)
        self.session.add(obj)
        await self.session.commit()
        await self.session.refresh(obj)
        return obj

    async def update(self, id: int, **kwargs) -> Optional[ModelType]:
        await self.session.execute(
            update(self.model).where(self.model.id == id).values(**kwargs)
        )
        await self.session.commit()
        return await self.get_by_id(id)

class UserRepository(BaseRepository[User]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, User)

    async def get_or_create(self, user_id: int, username: str | None = None) -> User:
        user = await self.get_by_id(user_id)
        if not user:
            user = await self.create(id=user_id, username=username)
        else:
            # Update username if it changed
            if username and user.username != username:
                user = await self.update(user_id, username=username)
        return user

class OrderRepository(BaseRepository[Order]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Order)

    async def get_pending_by_user(self, user_id: int) -> List[Order]:
        result = await self.session.execute(
            select(Order).filter(Order.user_id == user_id, Order.status == "pending")
        )
        return list(result.scalars().all())

class VPNProfileRepository(BaseRepository[VPNProfile]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, VPNProfile)

    async def get_by_user_id(self, user_id: int) -> Optional[VPNProfile]:
        result = await self.session.execute(
            select(VPNProfile).filter(VPNProfile.user_id == user_id, VPNProfile.status == "active")
        )
        return result.scalars().first()
