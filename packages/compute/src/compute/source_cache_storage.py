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

    Worker exit, worker-record removal, lease expiry, and elapsed time never call
    ``record_destroyed``. The owning provider lifecycle constructs evidence only
    after its authoritative lookup proves the node or machine storage is gone.
    """

    context: ComputeContext

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

    def record_machine_storage_destroyed(
        self,
        machine_id: str,
        *,
        observed_at: datetime,
    ) -> bool:
        """Retire the active cache for a provider-proven destroyed machine.

        A machine that never registered a cache has nothing to retire and is
        therefore complete. The active-storage uniqueness constraint guarantees
        there is at most one current generation for the machine owner.
        """

        with self.context.database.session() as session:
            return self.record_machine_storage_destroyed_in_session(
                session,
                machine_id,
                observed_at=observed_at,
            )

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
        targets = repository.list_targets(generation_ids=[generation.id])
        latest_owned_record_at = max(
            [generation.created_at, *(target.created_at for target in targets)]
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
        generations = [
            generation
            for generation in repository.list_generations(include_retired=True)
            if generation.storage_id == owner.storage_id
            and (generation_id is None or generation.id == generation_id)
        ]
        if not generations:
            raise NotFoundError(f"worker cache storage not found: {owner.storage_id}")
        active = [
            generation
            for generation in generations
            if generation.state is not WorkerCacheGenerationState.Retired
        ]
        if len(active) > 1:
            raise ConflictError(
                f"worker cache storage has multiple active generations: {owner.storage_id}"
            )
        if active:
            return active[0]
        return max(generations, key=lambda generation: (generation.updated_at, generation.id))

    @staticmethod
    def _snapshot(
        repository: SourceCacheCleanupRepository,
        *,
        owner: WorkerCacheStorageOwnerRecord,
        generation: WorkerCacheGenerationRecord,
    ) -> SourceCacheStorageCleanupStatusSnapshot:
        targets = repository.list_targets(generation_ids=[generation.id])
        pending_count = sum(target.status is SourceCacheCleanupStatus.Pending for target in targets)
        claimed_count = sum(target.status is SourceCacheCleanupStatus.Claimed for target in targets)
        completed_count = sum(
            target.status is SourceCacheCleanupStatus.Completed for target in targets
        )
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
            pending_count=pending_count,
            claimed_count=claimed_count,
            completed_count=completed_count,
            storage_destroyed_at=generation.storage_destroyed_at,
            complete=complete,
        )


__all__ = [
    "SourceCacheStorageCleanupStatusSnapshot",
    "SourceCacheStorageLifecycleService",
]
