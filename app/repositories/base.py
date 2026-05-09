from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import TypeVar, Generic, Type, List, Optional, Any
import uuid
from app.models.base import Base

ModelType = TypeVar("ModelType", bound=Base)

class BaseRepository(Generic[ModelType]):
    def __init__(self, model: Type[ModelType], session: AsyncSession):
        self.model = model
        self.session = session

    async def get_by_id(self, id: uuid.UUID, gym_id: uuid.UUID) -> Optional[ModelType]:
        """Tenant-safe fetch by ID."""
        q = select(self.model).where(
            self.model.id == id,
            self.model.gym_id == gym_id
        )
        result = await self.session.execute(q)
        return await result.scalar_one_or_none()

    async def list(
        self, 
        gym_id: uuid.UUID, 
        skip: int = 0, 
        limit: int = 100, 
        **filters: Any
    ) -> List[ModelType]:
        """Tenant-safe list with pagination."""
        q = select(self.model).where(self.model.gym_id == gym_id)
        
        for k, v in filters.items():
            if v is not None and hasattr(self.model, k):
                q = q.where(getattr(self.model, k) == v)
        
        q = q.offset(skip).limit(limit)
        result = await self.session.execute(q)
        return list(result.scalars().all())

    async def count(self, gym_id: uuid.UUID, **filters: Any) -> int:
        """Tenant-safe count."""
        q = select(func.count()).where(self.model.gym_id == gym_id)
        for k, v in filters.items():
            if v is not None and hasattr(self.model, k):
                q = q.where(getattr(self.model, k) == v)
        result = await self.session.execute(q)
        return await result.scalar_one()

    async def create(self, obj: ModelType) -> ModelType:
        """Add object to session and flush."""
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def update(self, obj: ModelType) -> ModelType:
        """Refresh object and return."""
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def delete(self, obj: ModelType) -> None:
        """Hard delete object."""
        await self.session.delete(obj)
        await self.session.flush()
