from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.timestamps import utc_now

# Runtime save allows 30 minutes and upload 5; reserve 10 for filesystem/archive work.
CHECKPOINT_OPERATION_TIMEOUT_SECONDS = 45 * 60.0
CHECKPOINT_REQUEST_TIMEOUT_SECONDS = CHECKPOINT_OPERATION_TIMEOUT_SECONDS + 30.0


class CheckpointStatus(StringEnum):
    Pending = "pending"
    Available = "available"
    Failed = "failed"
    CheckpointFailed = "checkpoint-failed"
    RestoreFailed = "restore-failed"


CHECKPOINT_RETENTION_ELIGIBLE_STATUSES = frozenset(
    {
        CheckpointStatus.Available,
        CheckpointStatus.Failed,
        CheckpointStatus.CheckpointFailed,
        CheckpointStatus.RestoreFailed,
    }
)


class AutomaticCheckpointCreationLease(ContractModel):
    acquired: bool = False
    available_checkpoint_id: str = ""


class CheckpointRecord(ContractModel):
    checkpoint_id: str
    source_container_id: str = ""
    container_ip: str = ""
    status: CheckpointStatus = CheckpointStatus.Pending
    remote_key: str = ""
    workspace_id: str = ""
    stub_id: str = ""
    stub_type: str = ""
    app_id: str = ""
    exposed_ports: list[int] = Field(default_factory=list)
    cache_hash: str = ""
    cache_size_bytes: int = 0
    origin_key: str = ""
    locality: str = ""
    accelerator: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_restored_at: datetime | None = None
    retention_expires_at: datetime | None = None
    cleanup_claimed_at: datetime | None = None
    deleted_at: datetime | None = None

    @field_validator("cache_size_bytes")
    @classmethod
    def cache_size_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "checkpoint cache size cannot be negative"
            raise ValueError(msg)
        return value


class CheckpointPruneResult(ContractModel):
    pruned: list[CheckpointRecord]

    @property
    def count(self) -> int:
        return len(self.pruned)


def checkpoint_recent_stub_key(workspace_id: str, stub_id: str) -> str:
    return f"{workspace_id}|{stub_id}"
