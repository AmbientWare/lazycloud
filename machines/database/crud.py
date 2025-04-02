from machines.database.session import session_manager
from machines.database.base import Base


async def create_tables():
    async with session_manager.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
