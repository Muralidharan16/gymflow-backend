import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.models.gym import FacilityType
from app.models.enums import FacilityType as FacilityTypeEnum
from app.core.config import settings

async def seed_facility_types():
    engine = create_async_engine(settings.DATABASE_URL)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async with AsyncSessionLocal() as session:
        # Initial data from Enum
        initial_types = [
            {"system_name": FacilityTypeEnum.gym, "display_name": "Gym / Fitness Center", "icon_key": "icon-gym"},
            {"system_name": FacilityTypeEnum.yoga_studio, "display_name": "Yoga Studio", "icon_key": "icon-yoga"},
            {"system_name": FacilityTypeEnum.crossfit_box, "display_name": "CrossFit Box", "icon_key": "icon-crossfit"},
            {"system_name": FacilityTypeEnum.swimming_pool, "display_name": "Swimming Pool", "icon_key": "icon-swimming"},
            {"system_name": FacilityTypeEnum.martial_arts, "display_name": "Martial Arts Academy", "icon_key": "icon-martial-arts"},
            {"system_name": FacilityTypeEnum.dance_studio, "display_name": "Dance Studio", "icon_key": "icon-dance"},
            {"system_name": FacilityTypeEnum.sports_academy, "display_name": "Sports Academy", "icon_key": "icon-sports"},
            {"system_name": FacilityTypeEnum.multi_sport, "display_name": "Multi-Sport Complex", "icon_key": "icon-multi-sport"},
            {"system_name": FacilityTypeEnum.others, "display_name": "Other Facility", "icon_key": "icon-other"},
        ]
        
        for t in initial_types:
            # Check if exists
            from sqlalchemy import select
            q = select(FacilityType).where(FacilityType.system_name == t["system_name"])
            res = await session.execute(q)
            if not res.scalar_one_or_none():
                facility_type = FacilityType(**t)
                session.add(facility_type)
        
        await session.commit()
    
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(seed_facility_types())
