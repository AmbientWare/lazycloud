from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass
from threading import RLock
from uuid import uuid4

from sqlalchemy import Engine, create_engine, event, literal, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from database.settings import DatabaseApplicationName, DatabaseSettings
from database.tables import DatabaseBase


@dataclass(slots=True)
class DatabaseClient:
    settings: DatabaseSettings
    engine: Engine
    sessions: sessionmaker[Session]
    session_lock: AbstractContextManager[object] | None = None

    @classmethod
    def from_settings(cls, settings: DatabaseSettings) -> DatabaseClient:
        config = settings
        engine = create_engine(config.url, **_engine_kwargs(config))
        _install_sqlite_uuid_function(engine, config.url)
        return cls(
            settings=config,
            engine=engine,
            sessions=sessionmaker(bind=engine, expire_on_commit=False),
            session_lock=RLock() if config.url.startswith("sqlite") else None,
        )

    @contextmanager
    def session(self) -> Iterator[Session]:
        with _optional_lock(self.session_lock):
            session = self.sessions()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    def create_schema(self) -> None:
        if self.engine.dialect.name == "postgresql":
            with self.engine.begin() as connection:
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
                DatabaseBase.metadata.create_all(connection)
            return
        DatabaseBase.metadata.create_all(self.engine)

    def ping(self) -> bool:
        with self.engine.connect() as connection:
            return connection.scalar(select(literal(1))) == 1

    def dispose(self) -> None:
        self.engine.dispose()


@dataclass(slots=True)
class AsyncDatabaseClient:
    settings: DatabaseSettings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

    @classmethod
    def from_settings(cls, settings: DatabaseSettings) -> AsyncDatabaseClient:
        config = settings
        engine = create_async_engine(_async_url(config.url), **_async_engine_kwargs(config))
        _install_sqlite_uuid_function(engine.sync_engine, config.url)
        return cls(
            settings=config,
            engine=engine,
            sessions=async_sessionmaker(bind=engine, expire_on_commit=False),
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        session = self.sessions()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
            if self.engine.dialect.name == "postgresql":
                await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
            await connection.run_sync(DatabaseBase.metadata.create_all)

    async def ping(self) -> bool:
        async with self.engine.connect() as connection:
            return await connection.scalar(select(literal(1))) == 1

    async def dispose(self) -> None:
        await self.engine.dispose()


def _engine_kwargs(settings: DatabaseSettings) -> dict[str, object]:
    if settings.url.startswith("sqlite"):
        kwargs: dict[str, object] = {"connect_args": {"check_same_thread": False}}
        if settings.url.endswith(":memory:"):
            kwargs["poolclass"] = StaticPool
        return kwargs

    options: list[str] = []
    if settings.statement_timeout_ms > 0:
        options.extend(("-c", f"statement_timeout={settings.statement_timeout_ms}"))
    if settings.application_name is DatabaseApplicationName.Wait:
        options.extend(("-c", "default_transaction_read_only=on"))
    connect_args: dict[str, object] = {
        "application_name": settings.application_name.value,
        "connect_timeout": settings.connect_timeout_seconds,
    }
    if options:
        connect_args["options"] = " ".join(options)
    return {
        "connect_args": connect_args,
        "echo": settings.echo,
        "pool_size": settings.pool_size,
        "max_overflow": settings.max_overflow,
        "pool_pre_ping": True,
    }


def _async_engine_kwargs(settings: DatabaseSettings) -> dict[str, object]:
    if settings.url.startswith("sqlite"):
        kwargs: dict[str, object] = {"connect_args": {"check_same_thread": False}}
        if settings.url.endswith(":memory:"):
            kwargs["poolclass"] = StaticPool
        return kwargs

    options: list[str] = []
    if settings.statement_timeout_ms > 0:
        options.extend(("-c", f"statement_timeout={settings.statement_timeout_ms}"))
    if settings.application_name is DatabaseApplicationName.Wait:
        options.extend(("-c", "default_transaction_read_only=on"))
    connect_args: dict[str, object] = {
        "application_name": settings.application_name.value,
        "connect_timeout": settings.connect_timeout_seconds,
    }
    if options:
        connect_args["options"] = " ".join(options)
    return {
        "connect_args": connect_args,
        "echo": settings.echo,
        "pool_size": settings.pool_size,
        "max_overflow": settings.max_overflow,
        "pool_pre_ping": True,
    }


def _async_url(url: str) -> str:
    if url.startswith("postgresql+psycopg_async://"):
        return url
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql+psycopg_async://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg_async://", 1)
    if url.startswith("sqlite+aiosqlite://"):
        return url
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


def _optional_lock(
    lock: AbstractContextManager[object] | None,
) -> AbstractContextManager[object | None]:
    return nullcontext() if lock is None else lock


def _install_sqlite_uuid_function(engine: Engine, url: str) -> None:
    if not url.startswith("sqlite"):
        return

    @event.listens_for(engine, "connect")
    def connect(dbapi_connection: object, _connection_record: object) -> None:
        execute = getattr(dbapi_connection, "execute", None)
        if execute is not None:
            execute("PRAGMA foreign_keys=ON")
        create_function = getattr(dbapi_connection, "create_function", None)
        if create_function is not None:
            create_function("gen_random_uuid", 0, lambda: uuid4().hex)
