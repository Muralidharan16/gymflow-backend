from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from typing import List, Tuple, Any
from app.models.geo import PostalCode, City, Subdivision, Country

class GeoRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_postal_lookups(self, country_iso2: str, postal_code: str) -> List[Any]:
        """
        Executes a canonical postal code lookup joining the immutable hierarchy.
        Returns tuples of (PostalCode, City, Subdivision, Country).
        """
        query = (
            select(PostalCode, City, Subdivision, Country)
            .join(City, PostalCode.city_id == City.id)
            .join(Subdivision, PostalCode.subdivision_id == Subdivision.id)
            .join(Country, PostalCode.country_id == Country.id)
            .where(
                and_(
                    Country.iso2 == country_iso2,
                    PostalCode.postal_code == postal_code,
                    PostalCode.status == 'active'
                )
            )
        )
        result = await self.db.execute(query)
        return result.all()

    async def get_countries(self) -> List[Country]:
        query = select(Country).where(Country.status == 'active').order_by(Country.name)
        result = await self.db.execute(query)
        return list(result.scalars().all())
