from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.repositories.source_cache import SourceCacheCleanupRepository
from database.types import DatabaseSession
from shared.errors import ConflictError, NotFoundError
from shared.source_cache_cleanup import (
    SourceCacheCleanupStatus,
    WorkerCacheGenerationRecord,
    WorkerCacheGenerationState,
    WorkerCacheStorageDestructionEvidence,
    WorkerCacheStorageOwnerKind,
    WorkerCacheStorageOwnerRecord,
)

from compute.context import ComputeContext


@dataclass(frozen=True, slots=True)
class SourceCacheStorageCleanupStatusSnapshot:
    """Exact durable cleanup state for one node- or machine-owned cache."""

    owner: WorkerCacheStorageOwnerRecord
    generation_id: str
    worker_id: str
    session_fence: int
    state: WorkerCacheGenerationState
    pending_count: int
    claimed_count: int
    completed_count: int
    storage_destroyed_at: datetime | None
    complete: bool


@dataclass(slots=True)
class SourceCacheStorageLifecycleService:
    """Physical cache lifecycle owned by machine/node provisioning.

    Retirement requires provider proof that storage is gone, or a fenced agent
    receipt after deleting the machine-owned cache before a retained stop.
    Worker exit and lease expiry do not prove that cached files are gone.
    """

    context: ComputeContext

    def acknowledge_machine_cleanup(
        self,
        *,
        machine_id: str,
        worker_id: str,
        generation_id: str,
        session_fence: int | None,
        observed_at: datetime,
    ) -> None:
        owner = WorkerCacheStorageOwnerRecord(
            kind=WorkerCacheStorageOwnerKind.Machine, owner_id=machine_id
        )
        try:
            current = self.get(owner)
        except NotFoundError:
            if generation_id or session_fence is not None:
                raise ConflictError("cache cleanup has no current server generation") from None
            return
        if current.complete:
            return
        if not generation_id or session_fence is None:
            raise ConflictError("machine-owned source cache must be cleaned before stopping")
        if (
            current.generation_id != generation_id
            or current.session_fence != session_fence
            or current.worker_id != worker_id
        ):
            raise ConflictError("cache cleanup acknowledgment is not the current machine session")
        destroyed = self.record_destroyed(
            WorkerCacheStorageDestructionEvidence(
                owner=owner, generation_id=generation_id, observed_at=observed_at
            )
        )
        if not destroyed.complete:
            raise ConflictError("machine-owned source cache cleanup is incomplete")

    def get(
        self,
        owner: WorkerCacheStorageOwnerRecord,
        *,
        generation_id: str | None = None,
    ) -> SourceCacheStorageCleanupStatusSnapshot:
        with self.context.database.session() as session:
            repository = SourceCacheCleanupRepository(session)
            generation = self._resolve_generation(
                repository,
                owner=owner,
                generation_id=generation_id,
            )
            return self._snapshot(repository, owner=owner, generation=generation)

    def record_machine_storage_destroyed_in_session(
        self,
        session: DatabaseSession,
        machine_id: str,
        *,
        observed_at: datetime,
    ) -> bool:
        owner = WorkerCacheStorageOwnerRecord(
            kind=WorkerCacheStorageOwnerKind.Machine,
            owner_id=machine_id,
        )
        repository = SourceCacheCleanupRepository(session)
        try:
            current = self._resolve_generation(
                repository,
                owner=owner,
                generation_id=None,
            )
        except NotFoundError:
            return True
        return self._record_destroyed(
            repository,
            WorkerCacheStorageDestructionEvidence(
                owner=owner,
                generation_id=current.id,
                observed_at=observed_at,
            ),
        ).complete

    def record_destroyed(
        self,
        evidence: WorkerCacheStorageDestructionEvidence,
    ) -> SourceCacheStorageCleanupStatusSnapshot:
        with self.context.database.session() as session:
            return self._record_destroyed(
                SourceCacheCleanupRepository(session),
                evidence,
            )

    @classmethod
    def _record_destroyed(
        cls,
        repository: SourceCacheCleanupRepository,
        evidence: WorkerCacheStorageDestructionEvidence,
    ) -> SourceCacheStorageCleanupStatusSnapshot:
        generation = cls._resolve_generation(
            repository,
            owner=evidence.owner,
            generation_id=evidence.generation_id,
        )
        latest_owned_record_at = max(
            generation.created_at,
            repository.latest_cleanup_target_at(generation.id) or generation.created_at,
        )
        if evidence.observed_at < latest_owned_record_at:
            raise ConflictError(
                "worker cache destruction evidence predates the owned cache generation"
            )
        retired = repository.retire_destroyed(
            generation.id,
            worker_id=generation.worker_id,
            storage_id=evidence.owner.storage_id,
            session_fence=generation.session_fence,
            now=evidence.observed_at,
        )
        if retired is None:
            raise ConflictError("worker cache generation changed before retirement")
        return cls._snapshot(repository, owner=evidence.owner, generation=retired)

    @staticmethod
    def _resolve_generation(
        repository: SourceCacheCleanupRepository,
        *,
        owner: WorkerCacheStorageOwnerRecord,
        generation_id: str | None,
    ) -> WorkerCacheGenerationRecord:
        generation = repository.generation_for_storage(
            owner.storage_id, generation_id=generation_id
        )
        if generation is None:
            raise NotFoundError(f"worker cache storage not found: {owner.storage_id}")
        return generation

    @staticmethod
    def _snapshot(
        repository: SourceCacheCleanupRepository,
        *,
        owner: WorkerCacheStorageOwnerRecord,
        generation: WorkerCacheGenerationRecord,
    ) -> SourceCacheStorageCleanupStatusSnapshot:
        counts = repository.generation_cleanup_counts(generation.id)
        complete = (
            generation.state is WorkerCacheGenerationState.Retired
            and generation.storage_destroyed_at is not None
        )
        return SourceCacheStorageCleanupStatusSnapshot(
            owner=owner,
            generation_id=generation.id,
            worker_id=generation.worker_id,
            session_fence=generation.session_fence,
            state=generation.state,
            pending_count=counts.get(SourceCacheCleanupStatus.Pending, 0),
            claimed_count=counts.get(SourceCacheCleanupStatus.Claimed, 0),
            completed_count=counts.get(SourceCacheCleanupStatus.Completed, 0),
            storage_destroyed_at=generation.storage_destroyed_at,
            complete=complete,
        )


__all__ = [
    "SourceCacheStorageCleanupStatusSnapshot",
    "SourceCacheStorageLifecycleService",
]
