from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from database.context import ServiceContext
from database.repositories.identity import WorkspaceRepository
from database.repositories.source_cache import SourceCacheCleanupRepository
from shared.errors import NotFoundError
from shared.source_cache_cleanup import SourceCacheCleanupErrorCode
from shared.timestamps import utc_now


@dataclass(frozen=True, slots=True)
class SourceCacheCleanupStatusSnapshot:
    workspace_id: str
    pending_count: int
    claimed_count: int
    completed_count: int
    generations_pending: int
    failing_count: int
    last_error_code: SourceCacheCleanupErrorCode | None
    oldest_pending_age_seconds: int | None
    complete: bool


@dataclass(slots=True)
class SourceCacheCleanupStatusService:
    context: ServiceContext

    def get(
        self,
        workspace_id_or_name: str,
        *,
        now: datetime | None = None,
    ) -> SourceCacheCleanupStatusSnapshot:
        with self.context.database.session() as session:
            workspace = WorkspaceRepository(session).resolve_for_deletion(workspace_id_or_name)
            if workspace is None:
                raise NotFoundError(f"workspace not found: {workspace_id_or_name}")
            summary = SourceCacheCleanupRepository(session).summarize(workspace_id=workspace.id)

        oldest_pending_age_seconds = None
        if summary.oldest_pending_at is not None:
            observed_at = _as_utc(now or utc_now())
            oldest_pending_at = _as_utc(summary.oldest_pending_at)
            oldest_pending_age_seconds = max(
                0,
                int((observed_at - oldest_pending_at).total_seconds()),
            )
        return SourceCacheCleanupStatusSnapshot(
            workspace_id=summary.workspace_id,
            pending_count=summary.pending_count,
            claimed_count=summary.claimed_count,
            completed_count=summary.completed_count,
            generations_pending=summary.generations_pending,
            failing_count=summary.failing_count,
            last_error_code=summary.last_error_code,
            oldest_pending_age_seconds=oldest_pending_age_seconds,
            complete=summary.complete,
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "SourceCacheCleanupStatusService",
    "SourceCacheCleanupStatusSnapshot",
]
