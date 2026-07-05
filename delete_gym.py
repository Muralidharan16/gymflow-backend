import asyncio
import os
import argparse
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.orm import sessionmaker

# Import your models
from app.models.models import Gym, GymOwner

load_dotenv()

def require_destructive_reset_enabled():
    if os.getenv("ALLOW_DESTRUCTIVE_DB_RESET") != "true":
        raise SystemExit("Refusing destructive gym deletion. Set ALLOW_DESTRUCTIVE_DB_RESET=true to continue.")

async def delete_gym_data(email: str = None, gymu_id: str = None):
    if not email and not gymu_id:
        print("Error: Please provide either an email or a gymu_id.")
        return

    engine = create_async_engine(os.getenv("DATABASE_URL"), echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        async with session.begin():
            # 1. Find the Gym ID first
            gym_to_delete = None
            
            if email:
                print(f"Looking up gym for owner email: {email}")
                stmt = select(GymOwner).where(GymOwner.email == email)
                result = await session.execute(stmt)
                owner = result.scalars().first()
                if not owner:
                    print(f"No owner found with email: {email}")
                    return
                
                gym_stmt = select(Gym).where(Gym.id == owner.gym_id)
                gym_result = await session.execute(gym_stmt)
                gym_to_delete = gym_result.scalars().first()

            elif gymu_id:
                print(f"Looking up gym by gymu_id: {gymu_id}")
                gym_stmt = select(Gym).where(Gym.gymu_id == gymu_id)
                gym_result = await session.execute(gym_stmt)
                gym_to_delete = gym_result.scalars().first()

            if not gym_to_delete:
                print("No associated gym found.")
                return

            print(f"Found Gym: {gym_to_delete.name} (UUID: {gym_to_delete.id}, Short ID: {gym_to_delete.gymu_id})")
            
            # 2. Delete the Gym
            # Because of ON DELETE CASCADE in your models (e.g. GymOwner, Member, Device, etc),
            # deleting the Gym will automatically wipe out all related records.
            confirm = input(f"Are you sure you want to PERMANENTLY delete '{gym_to_delete.name}' and ALL its data? (y/N): ")
            if confirm.lower() != 'y':
                print("Deletion cancelled.")
                return

            await session.execute(delete(Gym).where(Gym.id == gym_to_delete.id))
            print("Gym and all related owner, member, and token data successfully deleted.")

if __name__ == "__main__":
    require_destructive_reset_enabled()
    parser = argparse.ArgumentParser(description="Delete a gym and all its related owner data.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--email", type=str, help="The email of the gym owner")
    group.add_argument("--gymu_id", type=str, help="The short ID of the gym (e.g. FIT12345)")

    args = parser.parse_args()
    
    asyncio.run(delete_gym_data(email=args.email, gymu_id=args.gymu_id))
