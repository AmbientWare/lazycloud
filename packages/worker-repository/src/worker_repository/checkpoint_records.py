"""Server-side checkpoint persistence and lease services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from coordination.redis_client import RedisClient
from database.repositories.images import CheckpointRepository
from shared.checkpoints import (
    AutomaticCheckpointCreationLease,
    CheckpointPruneResult,
    CheckpointRecord,
    CheckpointStatus,
)
from shared.errors import ConflictError, NotFoundError
from shared.timestamps import utc_now
from worker.checkpoints import CheckpointStateOperation, CheckpointStatePayload

from database import DatabaseClient

DEFAULT_DURABLE_CHECKPOINT_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_CHECKPOINT_RESTORE_LEASE_SECONDS = 30 * 60
AUTOMATIC_CHECKPOINT_LEASE_NAMESPACE = "automatic-checkpoint-creation"
_AUTOMATIC_CHECKPOINT_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


def _validate_automatic_checkpoint_lease_inputs(
    *,
    workspace_id: str,
    stub_id: str,
    owner_token: str,
    ttl_seconds: int,
) -> None:
    if not workspace_id or not stub_id or not owner_token:
        raise ValueError("automatic checkpoint lease identity is incomplete")
    if ttl_seconds <= 0:
        raise ValueError("automatic checkpoint lease TTL must be positive")


@dataclass(slots=True)
class AutomaticCheckpointCreationLeaseService:
    context: CheckpointRecordContext
    redis: RedisClient

    def acquire(
        self,
        *,
        workspace_id: str,
        stub_id: str,
        owner_token: str,
        ttl_seconds: int,
    ) -> AutomaticCheckpointCreationLease:
        _validate_automatic_checkpoint_lease_inputs(
            workspace_id=workspace_id,
            stub_id=stub_id,
            owner_token=owner_token,
            ttl_seconds=ttl_seconds,
        )
        key = self._key(workspace_id, stub_id)
        acquired = bool(self.redis.set(key, owner_token, ex=ttl_seconds, nx=True))
        if not acquired:
            return AutomaticCheckpointCreationLease(
                available_checkpoint_id=self._latest_available(workspace_id, stub_id)
            )
        available_checkpoint_id = self._latest_available(workspace_id, stub_id)
        if available_checkpoint_id:
            self.release(
                workspace_id=workspace_id,
                stub_id=stub_id,
                owner_token=owner_token,
            )
            return AutomaticCheckpointCreationLease(available_checkpoint_id=available_checkpoint_id)
        return AutomaticCheckpointCreationLease(acquired=True)

    def release(
        self,
        *,
        workspace_id: str,
        stub_id: str,
        owner_token: str,
    ) -> bool:
        _validate_automatic_checkpoint_lease_inputs(
            workspace_id=workspace_id,
            stub_id=stub_id,
            owner_token=owner_token,
            ttl_seconds=1,
        )
        key = self._key(workspace_id, stub_id)
        return (
            self.redis.eval_int(
                _AUTOMATIC_CHECKPOINT_RELEASE_SCRIPT,
                1,
                key,
                owner_token,
            )
            == 1
        )

    def _latest_available(self, workspace_id: str, stub_id: str) -> str:
        with self.context.database.session() as session:
            checkpoint = CheckpointRepository(session).latest_available_for_stub(
                workspace_id=workspace_id,
                stub_id=stub_id,
            )
        return checkpoint.checkpoint_id if checkpoint is not None else ""

    def _key(self, workspace_id: str, stub_id: str) -> str:
        return self.redis.key(AUTOMATIC_CHECKPOINT_LEASE_NAMESPACE, workspace_id, stub_id)


class CheckpointRecordContextPaths(Protocol):
    @property
    def root(self) -> Path: ...


class CheckpointRecordContext(Protocol):
    @property
    def database(self) -> DatabaseClient: ...

    @property
    def paths(self) -> CheckpointRecordContextPaths: ...


class CheckpointRetention(Protocol):
    def prune(
        self,
        active_recent_stub_keys: list[str],
        *,
        now: datetime | None = None,
    ) -> CheckpointPruneResult: ...


@dataclass(slots=True)
class CheckpointService:
    context: CheckpointRecordContext
    retention: CheckpointRetention | None = None
    retention_seconds: int = DEFAULT_DURABLE_CHECKPOINT_RETENTION_SECONDS
    restore_lease_seconds: int = DEFAULT_CHECKPOINT_RESTORE_LEASE_SECONDS

    def save_state(self, payload: CheckpointStatePayload) -> CheckpointRecord:
        with self.context.database.session() as session:
            repository = CheckpointRepository(session)
            existing = repository.get_across_workspaces(
                payload.checkpoint_id,
                include_deleted=True,
            )
            record = checkpoint_record_from_payload(
                payload,
                existing,
                retention_seconds=self.retention_seconds,
            )
            return repository.upsert(record)

    def get_for_restore(self, checkpoint_id: str, *, workspace_id: str) -> CheckpointRecord:
        with self.context.database.session() as session:
            repository = CheckpointRepository(session)
            record = repository.get(checkpoint_id, workspace_id=workspace_id)
            if record is None:
                raise NotFoundError(f"checkpoint not found: {checkpoint_id}")
            if record.status is not CheckpointStatus.Available:
                raise ConflictError(f"checkpoint is not available: {checkpoint_id}")
            now = utc_now()
            restore_lease_expires_at = now + timedelta(seconds=self.restore_lease_seconds)
            retained = record.model_copy(
                update={
                    "retention_expires_at": max(
                        record.retention_expires_at or restore_lease_expires_at,
                        restore_lease_expires_at,
                    )
                }
            )
            return repository.upsert(retained)

    def get(self, checkpoint_id: str) -> CheckpointRecord | None:
        """Worker-side system lookup by checkpoint id."""
        with self.context.database.session() as session:
            return CheckpointRepository(session).get_across_workspaces(checkpoint_id)

    def prune_stale_cache_checkpoints(
        self,
        active_recent_stub_keys: list[str],
        *,
        now: datetime | None = None,
    ) -> CheckpointPruneResult:
        if self.retention is None:
            raise RuntimeError("durable checkpoint retention service is required")
        return self.retention.prune(active_recent_stub_keys, now=now)


def checkpoint_record_from_payload(
    payload: CheckpointStatePayload,
    existing: CheckpointRecord | None,
    *,
    retention_seconds: int = DEFAULT_DURABLE_CHECKPOINT_RETENTION_SECONDS,
) -> CheckpointRecord:
    now = utc_now()
    status = (
        CheckpointStatus(payload.status.value)
        if payload.status is not None
        else existing.status
        if existing is not None
        else CheckpointStatus.Pending
    )
    record = existing or CheckpointRecord(checkpoint_id=payload.checkpoint_id)
    retention_expires_at = record.retention_expires_at
    if status is CheckpointStatus.Pending:
        retention_expires_at = None
    elif status in {
        CheckpointStatus.Available,
        CheckpointStatus.Failed,
        CheckpointStatus.CheckpointFailed,
        CheckpointStatus.RestoreFailed,
    }:
        retention_expires_at = now + timedelta(seconds=retention_seconds)
    last_restored_at = record.last_restored_at
    if (
        payload.operation is CheckpointStateOperation.MarkRestored
        or payload.update_last_restored_at
    ):
        last_restored_at = now
    return CheckpointRecord(
        checkpoint_id=record.checkpoint_id,
        source_container_id=payload.source_container_id or record.source_container_id,
        container_ip=payload.container_ip or record.container_ip,
        status=status,
        remote_key=payload.remote_key or record.remote_key,
        workspace_id=payload.workspace_id or record.workspace_id,
        stub_id=payload.stub_id or record.stub_id,
        stub_type=payload.stub_type or record.stub_type,
        app_id=payload.app_id or record.app_id,
        exposed_ports=payload.exposed_ports or record.exposed_ports,
        cache_hash=payload.cache_hash or record.cache_hash,
        cache_size_bytes=payload.cache_size_bytes or record.cache_size_bytes,
        origin_key=payload.origin_key or record.origin_key,
        locality=payload.locality or record.locality,
        accelerator=payload.accelerator or record.accelerator,
        created_at=record.created_at,
        updated_at=now,
        last_restored_at=last_restored_at,
        retention_expires_at=retention_expires_at,
        cleanup_claimed_at=record.cleanup_claimed_at,
        deleted_at=record.deleted_at,
    )
