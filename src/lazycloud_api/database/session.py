from contextlib import asynccontextmanager
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool
from uuid import uuid4

from lazycloud_api.config import app_config

if app_config.IS_WORKER:
    DB_URL = app_config.DATABASE_POOL_URL
else:
    DB_URL = app_config.DATABASE_URL


# ensure asyncpg is used
if not DB_URL.startswith("postgresql+asyncpg"):
    DB_URL = DB_URL.replace("postgresql://", "postgresql+asyncpg://")


class DatabaseSessionManager:
    """Manages database connections and sessions"""

    def __init__(
        self,
        pool_size: int = 10,
        max_overflow: int = 20,
        pool_timeout: int = 30,
        pool_recycle: int = 1800,
    ):
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._pool_timeout = pool_timeout
        self._pool_recycle = pool_recycle

        self._initialize()

    def _initialize(self):
        connect_args = (
            {}
            if not app_config.IS_WORKER
            else {
                "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
                "statement_cache_size": 0,
                "prepared_statement_cache_size": 0,
            }
        )

        if app_config.IS_WORKER:
            self.engine = create_async_engine(
                DB_URL,
                poolclass=NullPool,
                future=True,
                connect_args=connect_args,
            )
        else:
            self.engine = create_async_engine(
                DB_URL,
                poolclass=AsyncAdaptedQueuePool,
                pool_size=self._pool_size,
                max_overflow=self._max_overflow,
                pool_timeout=self._pool_timeout,
                pool_recycle=self._pool_recycle,
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
