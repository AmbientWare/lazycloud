from contextlib import asynccontextmanager
from typing import AsyncGenerator
from uuid import uuid4

from loguru import logger
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

from lazycloud_api.config import app_config


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
        self._engine: AsyncEngine | None = None
        self._async_session: async_sessionmaker[AsyncSession] | None = None

    def _get_db_url(self) -> str:
        """Get database URL based on current config"""
        db_url = (
            app_config.DATABASE_POOL_URL
            if app_config.IS_WORKER
            else app_config.DATABASE_URL
        )
        if not db_url.startswith("postgresql+asyncpg"):
            db_url = db_url.replace("postgresql://", "postgresql+asyncpg://")

        return db_url

    def _ensure_initialized(self):
        """Lazy initialization of engine and session maker"""
        if self._engine is not None:
            return

        connect_args = (
            {}
            if not app_config.IS_WORKER
            else {
                "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
                "statement_cache_size": 0,
                "prepared_statement_cache_size": 0,
            }
        )

        db_url = self._get_db_url()

        if app_config.IS_WORKER:
            self._engine = create_async_engine(
                db_url,
                poolclass=NullPool,
                future=True,
                connect_args=connect_args,
            )

        else:
            self._engine = create_async_engine(
                db_url,
                poolclass=AsyncAdaptedQueuePool,
                pool_size=self._pool_size,
                max_overflow=self._max_overflow,
                pool_timeout=self._pool_timeout,
                pool_recycle=self._pool_recycle,
            )

        self._async_session = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        """Get the database engine, initializing if necessary"""
        self._ensure_initialized()
        assert self._engine is not None
        return self._engine

    @property
    def async_session(self) -> async_sessionmaker[AsyncSession]:
        """Get the async session maker, initializing if necessary"""
        self._ensure_initialized()
        assert self._async_session is not None
        return self._async_session

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """Get a database session"""
        self._ensure_initialized()
        assert self._async_session is not None
        session = self._async_session()
        try:
            yield session

        finally:
            await session.close()

    async def close(self):
        """Close all database connections"""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._async_session = None

    async def reset(self):
        """Reset the session manager (close and clear)"""
        await self.close()


# global instance
session_manager = DatabaseSessionManager()


async def shutdown_database():
    """Shutdown database connections"""

    try:
        await session_manager.close()
        logger.info("Database connections closed")

    except Exception as e:
        logger.warning(f"Error closing database connections: {e}")


async def reset_session():
    """Reset the global session manager (for testing)"""
    await session_manager.reset()
