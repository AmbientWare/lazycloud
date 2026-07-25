from __future__ import annotations

from datetime import UTC, datetime

from shared.source_cache_cleanup import (
    SourceCacheCleanupCompletionReason,
    SourceCacheCleanupErrorCode,
    SourceCacheCleanupStatus,
    SourceCacheCleanupTargetRecord,
    WorkerCacheGenerationRecord,
    WorkerCacheGenerationState,
)

from database.tables.source_cache import (
    SourceCacheCleanupTargetTable,
    WorkerCacheGenerationTable,
)


def worker_cache_generation_record_from_table(
    row: WorkerCacheGenerationTable,
) -> WorkerCacheGenerationRecord:
    return WorkerCacheGenerationRecord(
        id=str(row.id),
        worker_id=row.worker_id,
        storage_id=row.storage_id,
        workspace_id=str(row.workspace_id) if row.workspace_id is not None else None,
        state=WorkerCacheGenerationState(row.state),
        session_fence=row.session_fence,
        last_seen_at=_utc_datetime(row.last_seen_at),
        retired_at=_utc_datetime_or_none(row.retired_at),
        storage_destroyed_at=_utc_datetime_or_none(row.storage_destroyed_at),
        created_at=_utc_datetime(row.created_at),
        updated_at=_utc_datetime(row.updated_at),
    )


def source_cache_cleanup_target_record_from_table(
    row: SourceCacheCleanupTargetTable,
) -> SourceCacheCleanupTargetRecord:
    return SourceCacheCleanupTargetRecord(
        id=str(row.id),
        workspace_id=str(row.workspace_id),
        cache_generation_id=str(row.cache_generation_id),
        source_object_id=str(row.source_object_id),
        status=SourceCacheCleanupStatus(row.status),
        attempt_count=row.attempt_count,
        next_attempt_at=_utc_datetime(row.next_attempt_at),
        claim_token=row.claim_token,
        claim_expires_at=_utc_datetime_or_none(row.claim_expires_at),
        claim_session_fence=row.claim_session_fence,
        last_error_code=(
            SourceCacheCleanupErrorCode(row.last_error_code)
            if row.last_error_code is not None
            else None
        ),
        completed_at=_utc_datetime_or_none(row.completed_at),
        completion_reason=(
            SourceCacheCleanupCompletionReason(row.completion_reason)
            if row.completion_reason is not None
            else None
        ),
        created_at=_utc_datetime(row.created_at),
        updated_at=_utc_datetime(row.updated_at),
    )


def _utc_datetime(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _utc_datetime_or_none(value: datetime | None) -> datetime | None:
    return _utc_datetime(value) if value is not None else None


__all__ = [
    "source_cache_cleanup_target_record_from_table",
    "worker_cache_generation_record_from_table",
]
