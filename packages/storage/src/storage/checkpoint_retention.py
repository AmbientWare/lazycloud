from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.repositories.artifact_cleanup import (
    OBJECT_CLEANUP_CHECKPOINT,
    ArtifactCleanupRepository,
    object_location_lock_key,
)
from database.repositories.images import CheckpointRepository
from database.repositories.storage import ObjectRepository
from shared.checkpoints import (
    CHECKPOINT_RETENTION_ELIGIBLE_STATUSES,
    CheckpointPruneResult,
    CheckpointRecord,
    checkpoint_recent_stub_key,
)
from shared.timestamps import utc_now

from storage.context import StorageContext
from storage.service import ObjectStorage


@dataclass(slots=True)
class DurableCheckpointRetentionService:
    context: StorageContext
    object_storage: ObjectStorage
    checkpoint_bucket: str
    max_items_per_cycle: int = 100

    def prune(
        self,
        active_recent_stub_keys: list[str],
        *,
        now: datetime | None = None,
    ) -> CheckpointPruneResult:
        current = now or utc_now()
        with self.context.database.session() as session:
            claimed = ArtifactCleanupRepository(session).list_claimed_checkpoints(
                limit=self.max_items_per_cycle
            )
        pruned: list[CheckpointRecord] = []
        for checkpoint in claimed:
            pruned.extend(self._delete_claimed(checkpoint).pruned)
        with self.context.database.session() as session:
            candidates = CheckpointRepository(session).list_expired_for_retention(
                active_recent_stub_keys=active_recent_stub_keys,
                now=current,
                limit=max(self.max_items_per_cycle - len(pruned), 0),
            )
        for candidate in candidates:
            claimed_checkpoint = self._claim_candidate(
                candidate.checkpoint_id,
                active_recent_stub_keys=active_recent_stub_keys,
                now=current,
            )
            if claimed_checkpoint is not None:
                pruned.extend(self._delete_claimed(claimed_checkpoint).pruned)
        return CheckpointPruneResult(pruned=pruned)

    def _claim_candidate(
        self,
        checkpoint_id: str,
        *,
        active_recent_stub_keys: list[str],
        now: datetime,
    ) -> CheckpointRecord | None:
        active_keys = set(active_recent_stub_keys)
        with self.context.database.session() as session:
            claims = ArtifactCleanupRepository(session)
            claims.lock_keys({f"checkpoint:{checkpoint_id}"})
            checkpoints = CheckpointRepository(session)
            checkpoint = checkpoints.get_across_workspaces(checkpoint_id, include_claimed=True)
            if (
                checkpoint is None
                or checkpoint.cleanup_claimed_at is not None
                or checkpoint.retention_expires_at is None
                or checkpoint.retention_expires_at > now
                or checkpoint_recent_stub_key(checkpoint.workspace_id, checkpoint.stub_id)
                in active_keys
            ):
                return None
            if checkpoint.status not in CHECKPOINT_RETENTION_ELIGIBLE_STATUSES:
                return None
            if checkpoint.origin_key and not checkpoint.workspace_id:
                raise RuntimeError(
                    f"checkpoint workspace is required before pruning: {checkpoint.checkpoint_id}"
                )

            retained_origin = bool(
                checkpoint.origin_key
                and checkpoints.origin_is_referenced_elsewhere(
                    workspace_id=checkpoint.workspace_id,
                    origin_key=checkpoint.origin_key,
                    checkpoint_id=checkpoint.checkpoint_id,
                )
            )
            if checkpoint.origin_key and not retained_origin:
                objects = ObjectRepository(session)
                owned = objects.get_by_bucket_key(
                    self.checkpoint_bucket,
                    checkpoint.origin_key,
                    workspace_id=checkpoint.workspace_id,
                )
                claim_keys = {
                    object_location_lock_key(
                        checkpoint.workspace_id,
                        self.checkpoint_bucket,
                        checkpoint.origin_key,
                    )
                }
                if owned is not None:
                    claim_keys.add(f"object:{owned.id}")
                claims.lock_keys(claim_keys)
                if owned is not None:
                    claims.mark_object_claimed(
                        owned.id,
                        claimed_at=utc_now(),
                        cleanup_kind=OBJECT_CLEANUP_CHECKPOINT,
                    )
            return claims.mark_checkpoint_claimed(
                checkpoint.checkpoint_id,
                claimed_at=utc_now(),
            )

    def _delete_claimed(self, checkpoint: CheckpointRecord) -> CheckpointPruneResult:
        owned_id = ""
        retained_origin = False
        if checkpoint.origin_key:
            with self.context.database.session() as session:
                checkpoints = CheckpointRepository(session)
                retained_origin = checkpoints.origin_is_referenced_elsewhere(
                    workspace_id=checkpoint.workspace_id,
                    origin_key=checkpoint.origin_key,
                    checkpoint_id=checkpoint.checkpoint_id,
                )
                objects = ObjectRepository(session)
                owned = objects.get_by_bucket_key(
                    self.checkpoint_bucket,
                    checkpoint.origin_key,
                    workspace_id=checkpoint.workspace_id,
                    include_operations=True,
                )
                owned_id = owned.id if owned is not None else ""
            if not retained_origin:
                physical_key = self.object_storage.physical_key_for_workspace(
                    checkpoint.workspace_id,
                    bucket=self.checkpoint_bucket,
                    key=checkpoint.origin_key,
                )
                physical_bucket = self.object_storage.physical_bucket(self.checkpoint_bucket)
                self.object_storage.object_client.delete(
                    physical_key,
                    bucket=physical_bucket,
                )
                if self.object_storage.object_client.exists(
                    physical_key,
                    bucket=physical_bucket,
                ):
                    raise RuntimeError(
                        f"checkpoint origin deletion was not confirmed: {checkpoint.origin_key}"
                    )

        with self.context.database.session() as session:
            claims = ArtifactCleanupRepository(session)
            keys = {f"checkpoint:{checkpoint.checkpoint_id}"}
            if checkpoint.origin_key:
                keys.add(
                    object_location_lock_key(
                        checkpoint.workspace_id,
                        self.checkpoint_bucket,
                        checkpoint.origin_key,
                    )
                )
            if owned_id:
                keys.add(f"object:{owned_id}")
            claims.lock_keys(keys)
            current = CheckpointRepository(session).get_across_workspaces(
                checkpoint.checkpoint_id,
                include_claimed=True,
            )
            if current is None:
                return CheckpointPruneResult(pruned=[])
            if current.cleanup_claimed_at is None:
                raise RuntimeError(f"checkpoint cleanup claim was lost: {checkpoint.checkpoint_id}")
            if owned_id and not retained_origin:
                ObjectRepository(session).delete_across_workspaces(owned_id)
            return CheckpointRepository(session).prune([checkpoint.checkpoint_id])


__all__ = ["DurableCheckpointRetentionService"]
