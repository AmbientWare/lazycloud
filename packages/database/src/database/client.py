from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass, field
from threading import RLock
from uuid import uuid4

from shared.errors import UpstreamUnavailableError
from sqlalchemy import Engine, create_engine, event, literal, select, text
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import Pool, QueuePool, StaticPool

from database.settings import DatabaseApplicationName, DatabaseSettings
from database.tables import DatabaseBase

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DatabasePoolStatus:
    checked_out: int
    capacity: int
    exhaustions_total: int = 0

    @property
    def available(self) -> int:
        return max(self.capacity - self.checked_out, 0)

    @property
    def exhausted(self) -> bool:
        return self.checked_out >= self.capacity


@dataclass(slots=True)
class DatabaseClient:
    settings: DatabaseSettings
    engine: Engine
    sessions: sessionmaker[Session]
    session_lock: AbstractContextManager[object] | None = None
    _pool_exhaustions: int = field(default=0, init=False, repr=False)

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
            session = self._checkout()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    def _checkout(self) -> Session:
        session = self.sessions()
        try:
            session.connection()
        except PoolTimeout as exc:
            session.close()
            raise self._pool_exhausted(exc) from exc
        return session

    def _pool_exhausted(self, exc: BaseException) -> UpstreamUnavailableError:
        self._pool_exhaustions += 1
        return _pool_exhausted_error(self.settings, self.engine.pool.status(), exc)

    def create_schema(self) -> None:
        if self.engine.dialect.name == "postgresql":
            with self.engine.begin() as connection:
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
                DatabaseBase.metadata.create_all(connection)
            return
        DatabaseBase.metadata.create_all(self.engine)

    def ping(self) -> bool:
        try:
            with self.engine.connect() as connection:
                return connection.scalar(select(literal(1))) == 1
        except PoolTimeout as exc:
            raise self._pool_exhausted(exc) from exc

    def dispose(self) -> None:
        self.engine.dispose()

    def pool_status(self) -> DatabasePoolStatus | None:
        return _pool_status(self.engine.pool, self.settings, self._pool_exhaustions)


@dataclass(slots=True)
class AsyncDatabaseClient:
    settings: DatabaseSettings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    _pool_exhaustions: int = field(default=0, init=False, repr=False)

    @classmethod
    def from_settings(cls, settings: DatabaseSettings) -> AsyncDatabaseClient:
        config = settings
        engine = create_async_engine(config.url, **_engine_kwargs(config))
        _install_sqlite_uuid_function(engine.sync_engine, config.url)
        return cls(
            settings=config,
            engine=engine,
            sessions=async_sessionmaker(bind=engine, expire_on_commit=False),
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        session = await self._checkout()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def run_transaction[ResultT](
        self,
        operation: Callable[[Session], ResultT],
    ) -> ResultT:
        async with self.session() as session:
            return await session.run_sync(operation)

    async def _checkout(self) -> AsyncSession:
        session = self.sessions()
        try:
            await session.connection()
        except PoolTimeout as exc:
            await session.close()
            raise self._pool_exhausted(exc) from exc
        return session

    def _pool_exhausted(self, exc: BaseException) -> UpstreamUnavailableError:
        self._pool_exhaustions += 1
        return _pool_exhausted_error(self.settings, self.engine.sync_engine.pool.status(), exc)

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
            if self.engine.dialect.name == "postgresql":
                await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
            await connection.run_sync(DatabaseBase.metadata.create_all)

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                return await connection.scalar(select(literal(1))) == 1
        except PoolTimeout as exc:
            raise self._pool_exhausted(exc) from exc

    async def dispose(self) -> None:
        await self.engine.dispose()

    def pool_status(self) -> DatabasePoolStatus | None:
        return _pool_status(self.engine.sync_engine.pool, self.settings, self._pool_exhaustions)


def _pool_status(
    pool: Pool,
    settings: DatabaseSettings,
    exhaustions: int,
) -> DatabasePoolStatus | None:
    if not isinstance(pool, QueuePool):
        return None
    return DatabasePoolStatus(
        checked_out=pool.checkedout(),
        capacity=settings.pool_size + settings.max_overflow,
        exhaustions_total=exhaustions,
    )


def _pool_exhausted_error(
    settings: DatabaseSettings,
    pool_status: str,
    exc: BaseException,
) -> UpstreamUnavailableError:
    """Name the pool, so exhaustion is not read as an unreachable database.

    The two look identical from a failed query and want opposite responses:
    one is fixed by waiting or shedding load, the other by looking at the
    database. Saying which, with the pool's own numbers, is the difference
    between a minute and an afternoon.
    """

    LOGGER.warning(
        "database pool exhausted for %s: %s (%s)",
        settings.application_name.value,
        pool_status,
        exc,
    )
    return UpstreamUnavailableError(
        f"database connections are exhausted for {settings.application_name.value}"
    )


def _engine_kwargs(settings: DatabaseSettings) -> dict[str, object]:
    if settings.url.startswith("sqlite"):
        # A file database serves a sync and an async engine at once; SQLite has
        # one writer, so the second waits rather than failing on a busy lock.
        kwargs: dict[str, object] = {
            "connect_args": {"check_same_thread": False, "timeout": 30},
        }
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
        "pool_timeout": settings.pool_timeout_seconds,
        "pool_recycle": settings.pool_recycle_seconds,
        "pool_use_lifo": settings.pool_use_lifo,
        "pool_pre_ping": True,
    }


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
