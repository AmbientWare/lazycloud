from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from shared.contracts import ContractModel
from shared.enums import StringEnum


class WorkerCacheGenerationState(StringEnum):
    Initializing = "initializing"
    Available = "available"
    Draining = "draining"
    Retired = "retired"


class WorkerCacheStorageOwnerKind(StringEnum):
    Machine = "machine"
    Node = "node"


class WorkerCacheStorageOwnerRecord(ContractModel):
    """Stable owner identity for cache bytes outside the worker process."""

    kind: WorkerCacheStorageOwnerKind
    owner_id: str = Field(min_length=1, max_length=500)

    @field_validator("owner_id")
    @classmethod
    def _normalize_owner_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("worker cache storage owner id is required")
        return normalized

    @property
    def storage_id(self) -> str:
        return f"{self.kind.value}:{self.owner_id}"

    @classmethod
    def from_storage_id(cls, storage_id: str) -> WorkerCacheStorageOwnerRecord:
        owner_kind, separator, owner_id = storage_id.strip().partition(":")
        if not separator or not owner_id.strip():
            raise ValueError("worker cache storage id must identify a machine or node owner")
        try:
            kind = WorkerCacheStorageOwnerKind(owner_kind)
        except ValueError as exc:
            raise ValueError(
                "worker cache storage id must identify a machine or node owner"
            ) from exc
        return cls(kind=kind, owner_id=owner_id)


class WorkerCacheStorageDestructionEvidence(ContractModel):
    """Authoritative node/machine owner observation that its cache storage is gone."""

    owner: WorkerCacheStorageOwnerRecord
    generation_id: str = Field(min_length=1, max_length=64)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _require_aware_observation(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("worker cache destruction evidence must be timezone-aware")
        return value


class SourceCacheCleanupStatus(StringEnum):
    Pending = "pending"
    Claimed = "claimed"
    Completed = "completed"


class SourceCacheCleanupCompletionReason(StringEnum):
    Purged = "purged"
    StorageDestroyed = "storage-destroyed"


class SourceCacheCleanupErrorCode(StringEnum):
    PurgeFailed = "purge-failed"


class WorkerCacheGenerationRecord(ContractModel):
    """Durable identity and session fence for one physical worker cache."""

    id: str
    worker_id: str
    storage_id: str
    workspace_id: str | None = None
    state: WorkerCacheGenerationState
    session_fence: int = Field(ge=1)
    last_seen_at: datetime
    retired_at: datetime | None = None
    storage_destroyed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class SourceCacheCleanupTargetRecord(ContractModel):
    """One source object that must be absent from one physical cache."""

    id: str
    workspace_id: str
    cache_generation_id: str
    source_object_id: str
    status: SourceCacheCleanupStatus
    attempt_count: int = Field(ge=0)
    next_attempt_at: datetime
    claim_token: str | None = None
    claim_expires_at: datetime | None = None
    claim_session_fence: int | None = Field(default=None, ge=1)
    last_error_code: SourceCacheCleanupErrorCode | None = None
    completed_at: datetime | None = None
    completion_reason: SourceCacheCleanupCompletionReason | None = None
    created_at: datetime
    updated_at: datetime


class SourceCacheCleanupSummary(ContractModel):
    workspace_id: str
    pending_count: int = Field(ge=0)
    claimed_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    generations_pending: int = Field(ge=0)
    oldest_pending_at: datetime | None = None
    complete: bool


__all__ = [
    "SourceCacheCleanupCompletionReason",
    "SourceCacheCleanupErrorCode",
    "SourceCacheCleanupStatus",
    "SourceCacheCleanupSummary",
    "SourceCacheCleanupTargetRecord",
    "WorkerCacheGenerationRecord",
    "WorkerCacheGenerationState",
    "WorkerCacheStorageDestructionEvidence",
    "WorkerCacheStorageOwnerKind",
    "WorkerCacheStorageOwnerRecord",
]
