from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import text

from database.client import DatabaseClient


@dataclass(slots=True)
class WorkspaceDeletionFence:
    """Serialize one workspace's complete deletion attempt across API replicas."""

    database: DatabaseClient

    @contextmanager
    def acquire(self, workspace_id: str) -> Iterator[None]:
        lock_key = f"workspace-deletion:{workspace_id}"
        with self.database.direct_connection() as connection:
            acquired = False
            try:
                connection.execute(
                    text("SELECT pg_advisory_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": lock_key},
                )
                acquired = True
                yield
            finally:
                if acquired:
                    connection.execute(
                        text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"),
                        {"lock_key": lock_key},
                    )


__all__ = ["WorkspaceDeletionFence"]
