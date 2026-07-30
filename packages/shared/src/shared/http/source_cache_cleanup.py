from __future__ import annotations

from pydantic import Field

from shared.http.base import HttpModel
from shared.source_cache_cleanup import SourceCacheCleanupErrorCode


class SourceCacheCleanupStatusResponse(HttpModel):
    """Bounded operator view of durable source-cache cleanup progress."""

    workspace_id: str
    pending_count: int = Field(ge=0)
    claimed_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    generations_pending: int = Field(ge=0)
    failing_count: int = Field(default=0, ge=0)
    last_error_code: SourceCacheCleanupErrorCode | None = None
    oldest_pending_age_seconds: int | None = Field(default=None, ge=0)
    complete: bool


__all__ = ["SourceCacheCleanupStatusResponse"]
