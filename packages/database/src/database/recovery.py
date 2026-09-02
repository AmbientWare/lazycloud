from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
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

    @property
    def supported(self) -> bool:
        return self.database.engine.dialect.name == "postgresql"

    def start_serving(self) -> None:
        if not self.supported or self._serving_connection is not None:
            return
        connection = self.database.engine.connect()
        try:
            connection.execute(
                text("SELECT pg_advisory_lock_shared(:lock_id)"),
                {"lock_id": _CONTROL_PLANE_RECOVERY_LOCK_ID},
            )
        except BaseException:
            connection.close()
            raise
        self._serving_connection = connection

    def stop_serving(self) -> None:
        connection = self._serving_connection
        self._serving_connection = None
        if connection is None:
            return
        try:
            connection.execute(
                text("SELECT pg_advisory_unlock_shared(:lock_id)"),
                {"lock_id": _CONTROL_PLANE_RECOVERY_LOCK_ID},
            )
        finally:
            connection.close()

    @contextmanager
    def offline_recovery(self) -> Iterator[bool]:
        if not self.supported:
            yield False
            return
        connection = self.database.engine.connect()
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
            connection.close()


@dataclass(slots=True)
class AsyncControlPlaneRecoveryFence:
    database: AsyncDatabaseClient
    _serving_connection: AsyncConnection | None = None

    @property
    def supported(self) -> bool:
        return self.database.engine.dialect.name == "postgresql"

    async def start_serving(self) -> None:
        if not self.supported or self._serving_connection is not None:
            return
        connection = await self.database.engine.connect()
        try:
            await connection.execute(
                text("SELECT pg_advisory_lock_shared(:lock_id)"),
                {"lock_id": _CONTROL_PLANE_RECOVERY_LOCK_ID},
            )
        except BaseException:
            await connection.close()
            raise
        self._serving_connection = connection

    async def stop_serving(self) -> None:
        connection = self._serving_connection
        self._serving_connection = None
        if connection is None:
            return
        try:
            await connection.execute(
                text("SELECT pg_advisory_unlock_shared(:lock_id)"),
                {"lock_id": _CONTROL_PLANE_RECOVERY_LOCK_ID},
            )
        finally:
            await connection.close()
