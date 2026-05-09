from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import TypeVar, Generic, Type, List, Optional
import uuid
from app.models.base import Base

ModelType = TypeVar("ModelType", bound=Base)

class BaseRepository(Generic[ModelType]):
    def __init__(self, model: Type[ModelType], session: AsyncSession):
        self.model = model
        self.session = session

    async def get_by_id(self, id: uuid.UUID, gym_id: uuid.UUID) -> Optional[ModelType]:
        result = await self.session.execute(
            select(self.model).where(
                self.model.id == id,
                self.model.gym_id == gym_id
            )
        )
        return result.scalar_one_or_none()

    async def list(self, gym_id: uuid.UUID, **filters) -> List[ModelType]:
        q = select(self.model).where(self.model.gym_id == gym_id)
        for k, v in filters.items():
            if v is not None and hasattr(self.model, k):
                q = q.where(getattr(self.model, k) == v)
        result = await self.session.execute(q)
        return list(result.scalars().all())

    async def create(self, obj: ModelType) -> ModelType:
        self.session.add(obj)
        await self.session.flush()
        await self.session.refresh(obj)
        return obj

    async def count(self, gym_id: uuid.UUID, **filters) -> int:
        q = select(func.count()).where(self.model.gym_id == gym_id)
        for k, v in filters.items():
            if v is not None and hasattr(self.model, k):
                q = q.where(getattr(self.model, k) == v)
        result = await self.session.execute(q)
        return result.scalar_one()

    async def update(self, obj: ModelType) -> ModelType:
        self.session.add(obj)
        await self.session.flush()
        await self.session.refresh(obj)
        return obj
