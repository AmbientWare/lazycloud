from contextlib import asynccontextmanager
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlalchemy import text

from machines.config import app_config

DATABASE_URL = app_config.DATABASE_URL
# ensure asyncpg is used
if not DATABASE_URL.startswith("postgresql+asyncpg"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")


class DatabaseSessionManager:
    """Manages database connections and sessions"""

    def __init__(
        self,
        pool_size: int = 10,
        max_overflow: int = 20,
        pool_timeout: int = 30,
        pool_recycle: int = 1800,
    ):
        self.engine = create_async_engine(
            DATABASE_URL,
            poolclass=AsyncAdaptedQueuePool,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
        )
        self.async_session = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """Get a database session"""
        session = self.async_session()
        try:
            yield session
        finally:
            await session.close()

    async def close(self):
        """Close all database connections"""
        await self.engine.dispose()


# Global session manager instance
session_manager = DatabaseSessionManager()
