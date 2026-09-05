from __future__ import annotations

from collections.abc import Iterator
from contextlib import AsyncExitStack, ExitStack, contextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection

from database.client import AsyncDatabaseClient, DatabaseClient

# Stable PostgreSQL advisory-lock namespace for control-plane recovery. Serving
# replicas hold a shared session lock; offline recovery must acquire the
# exclusive form before it may mint an administrator credential.
_CONTROL_PLANE_RECOVERY_LOCK_ID = 7_312_990_118_742_021_765


@dataclass(slots=True)
class ControlPlaneRecoveryFence:
    database: DatabaseClient
    _serving_connection: Connection | None = None
    _serving_stack: ExitStack | None = None

    @property
    def supported(self) -> bool:
        return self.database.engine.dialect.name == "postgresql"

    def start_serving(self) -> None:
        if not self.supported or self._serving_connection is not None:
            return
        stack = ExitStack()
        try:
            connection = stack.enter_context(self.database.direct_connection())
            connection.execute(
                text("SELECT pg_advisory_lock_shared(:lock_id)"),
                {"lock_id": _CONTROL_PLANE_RECOVERY_LOCK_ID},
            )
        except BaseException:
            stack.close()
            raise
        self._serving_connection = connection
        self._serving_stack = stack

    def stop_serving(self) -> None:
        connection = self._serving_connection
        stack = self._serving_stack
        self._serving_connection = None
        self._serving_stack = None
        if connection is None:
            return
        try:
            connection.execute(
                text("SELECT pg_advisory_unlock_shared(:lock_id)"),
                {"lock_id": _CONTROL_PLANE_RECOVERY_LOCK_ID},
            )
        finally:
            if stack is not None:
                stack.close()

    @contextmanager
    def offline_recovery(self) -> Iterator[bool]:
        if not self.supported:
            yield False
            return
        with self.database.direct_connection() as connection:
            acquired = False
            try:
                acquired = bool(
                    connection.scalar(
                        text("SELECT pg_try_advisory_lock(:lock_id)"),
                        {"lock_id": _CONTROL_PLANE_RECOVERY_LOCK_ID},
                    )
                )
                yield acquired
            finally:
                if acquired:
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_id)"),
                        {"lock_id": _CONTROL_PLANE_RECOVERY_LOCK_ID},
                    )


@dataclass(slots=True)
class AsyncControlPlaneRecoveryFence:
    database: AsyncDatabaseClient
    _serving_connection: AsyncConnection | None = None
    _serving_stack: AsyncExitStack | None = None

    @property
    def supported(self) -> bool:
        return self.database.engine.dialect.name == "postgresql"

    async def start_serving(self) -> None:
        if not self.supported or self._serving_connection is not None:
            return
        stack = AsyncExitStack()
        try:
            connection = await stack.enter_async_context(self.database.direct_connection())
            await connection.execute(
                text("SELECT pg_advisory_lock_shared(:lock_id)"),
                {"lock_id": _CONTROL_PLANE_RECOVERY_LOCK_ID},
            )
        except BaseException:
            await stack.aclose()
            raise
        self._serving_connection = connection
        self._serving_stack = stack

    async def stop_serving(self) -> None:
        connection = self._serving_connection
        stack = self._serving_stack
        self._serving_connection = None
        self._serving_stack = None
        if connection is None:
            return
        try:
            await connection.execute(
                text("SELECT pg_advisory_unlock_shared(:lock_id)"),
                {"lock_id": _CONTROL_PLANE_RECOVERY_LOCK_ID},
            )
        finally:
            if stack is not None:
                await stack.aclose()
