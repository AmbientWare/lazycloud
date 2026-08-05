from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from database.context import ServiceContext
from database.repositories.orchestration import WorkerRepository
from database.repositories.source_cache import SourceCacheCleanupRepository
from database.types import DatabaseSession
from identity.auth import AuthorizationDeniedError
from shared.errors import ConflictError, InvalidInputError, UpstreamUnavailableError
from shared.source_cache_cleanup import (
    SourceCacheCleanupErrorCode,
    SourceCacheCleanupTargetRecord,
    WorkerCacheGenerationRecord,
    WorkerCacheGenerationState,
    WorkerCacheStorageOwnerKind,
    WorkerCacheStorageOwnerRecord,
)
from shared.timestamps import utc_now
from worker.repository_payloads import WorkerRepositoryPrincipal

SOURCE_CACHE_CLAIM_LEASE_SECONDS = 30
SOURCE_CACHE_RETRY_DELAY_SECONDS = 5


class WorkerSourceCacheUnavailableError(UpstreamUnavailableError):
    pass


@dataclass(frozen=True, slots=True)
class WorkerSourceCacheClaim:
    targets: list[SourceCacheCleanupTargetRecord]
    generation_state: WorkerCacheGenerationState


@dataclass(slots=True)
class WorkerSourceCacheService:
    context: ServiceContext

    def register(
        self,
        *,
        principal: WorkerRepositoryPrincipal,
        worker_id: str,
        generation_id: str,
        storage_id: str,
    ) -> WorkerCacheGenerationRecord:
        self._authorize_worker(principal, worker_id, allow_bootstrap=True)
        now = utc_now()
        with self.context.database.session() as session:
            self._assert_storage_owner(session, worker_id=worker_id, storage_id=storage_id)
            return SourceCacheCleanupRepository(session).register_generation(
                generation_id,
                worker_id=worker_id,
                storage_id=storage_id,
                workspace_id=self._workspace_scope(principal),
                now=now,
            )

    @staticmethod
    def _assert_storage_owner(
        session: DatabaseSession,
        *,
        worker_id: str,
        storage_id: str,
    ) -> None:
        """Reject a storage id that cache retirement could never resolve.

        Retirement resolves a generation strictly by its owner identity, so a
        registration whose storage id names a different owner produces a generation
        no machine termination can retire, leaving that workspace's cleanup pending
        forever.
        """
        try:
            owner = WorkerCacheStorageOwnerRecord.from_storage_id(storage_id)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        if owner.kind is not WorkerCacheStorageOwnerKind.Machine:
            return
        worker = WorkerRepository(session).get_across_workspaces(worker_id)
        if worker is None or not worker.machine_id:
            return
        if owner.owner_id != worker.machine_id:
            raise InvalidInputError(
                "worker cache storage owner does not match the worker's machine"
            )

    def claim(
        self,
        *,
        principal: WorkerRepositoryPrincipal,
        worker_id: str,
        generation_id: str,
        session_fence: int,
        limit: int,
    ) -> WorkerSourceCacheClaim:
        self._authorize_worker(principal, worker_id)
        now = utc_now()
        with self.context.database.session() as session:
            repository = SourceCacheCleanupRepository(session)
            generation = self._require_generation(
                repository,
                principal=principal,
                worker_id=worker_id,
                generation_id=generation_id,
                session_fence=session_fence,
            )
            targets = repository.claim_due(
                generation_id,
                worker_id=worker_id,
                session_fence=session_fence,
                now=now,
                lease_until=now + timedelta(seconds=SOURCE_CACHE_CLAIM_LEASE_SECONDS),
                limit=limit,
            )
            return WorkerSourceCacheClaim(
                targets=targets,
                generation_state=(
                    WorkerCacheGenerationState.Draining
                    if targets and generation.state is WorkerCacheGenerationState.Available
                    else generation.state
                ),
            )

    def complete(
        self,
        *,
        principal: WorkerRepositoryPrincipal,
        worker_id: str,
        generation_id: str,
        session_fence: int,
        target_id: str,
        claim_token: str,
    ) -> None:
        self._authorize_worker(principal, worker_id)
        with self.context.database.session() as session:
            repository = SourceCacheCleanupRepository(session)
            self._require_generation(
                repository,
                principal=principal,
                worker_id=worker_id,
                generation_id=generation_id,
                session_fence=session_fence,
            )
            resolved = repository.complete_claim(
                target_id,
                generation_id=generation_id,
                worker_id=worker_id,
                session_fence=session_fence,
                claim_token=claim_token,
                now=utc_now(),
            )
            if not resolved:
                raise ConflictError("source cache cleanup claim is no longer current")

    def fail(
        self,
        *,
        principal: WorkerRepositoryPrincipal,
        worker_id: str,
        generation_id: str,
        session_fence: int,
        target_id: str,
        claim_token: str,
    ) -> None:
        self._authorize_worker(principal, worker_id)
        now = utc_now()
        with self.context.database.session() as session:
            repository = SourceCacheCleanupRepository(session)
            self._require_generation(
                repository,
                principal=principal,
                worker_id=worker_id,
                generation_id=generation_id,
                session_fence=session_fence,
            )
            resolved = repository.fail_claim(
                target_id,
                generation_id=generation_id,
                worker_id=worker_id,
                session_fence=session_fence,
                claim_token=claim_token,
                next_attempt_at=now + timedelta(seconds=SOURCE_CACHE_RETRY_DELAY_SECONDS),
                error_code=SourceCacheCleanupErrorCode.PurgeFailed,
                now=now,
            )
            if not resolved:
                raise ConflictError("source cache cleanup claim is no longer current")

    def activate(
        self,
        *,
        principal: WorkerRepositoryPrincipal,
        worker_id: str,
        generation_id: str,
        session_fence: int,
    ) -> WorkerCacheGenerationRecord:
        self._authorize_worker(principal, worker_id)
        with self.context.database.session() as session:
            repository = SourceCacheCleanupRepository(session)
            self._require_generation(
                repository,
                principal=principal,
                worker_id=worker_id,
                generation_id=generation_id,
                session_fence=session_fence,
            )
            generation = repository.activate_if_drained(
                generation_id,
                worker_id=worker_id,
                session_fence=session_fence,
                now=utc_now(),
            )
            if generation is None:
                raise ConflictError("source cache session is no longer current")
            return generation

    def require_available(
        self,
        *,
        principal: WorkerRepositoryPrincipal,
        worker_id: str,
        generation_id: str,
        session_fence: int,
    ) -> WorkerCacheGenerationRecord:
        generation = self.current(
            principal=principal,
            worker_id=worker_id,
            generation_id=generation_id,
            session_fence=session_fence,
        )
        if generation.state not in {
            WorkerCacheGenerationState.Available,
            WorkerCacheGenerationState.Draining,
        }:
            raise WorkerSourceCacheUnavailableError(
                f"worker source cache is {generation.state.value}: {worker_id}"
            )
        return generation

    def current(
        self,
        *,
        principal: WorkerRepositoryPrincipal,
        worker_id: str,
        generation_id: str,
        session_fence: int,
    ) -> WorkerCacheGenerationRecord:
        self._authorize_worker(principal, worker_id)
        now = utc_now()
        with self.context.database.session() as session:
            repository = SourceCacheCleanupRepository(session)
            generation = self._require_generation(
                repository,
                principal=principal,
                worker_id=worker_id,
                generation_id=generation_id,
                session_fence=session_fence,
            )
            if not repository.touch_generation(
                generation_id,
                worker_id=worker_id,
                session_fence=session_fence,
                now=now,
            ):
                raise ConflictError("worker source cache session is no longer current")
            return generation.model_copy(update={"last_seen_at": now, "updated_at": now})

    @staticmethod
    def _workspace_scope(principal: WorkerRepositoryPrincipal) -> str | None:
        if principal.is_private_worker:
            if not principal.workspace_id:
                raise AuthorizationDeniedError("private worker workspace is required")
            return principal.workspace_id
        if principal.is_managed_worker:
            return None
        raise AuthorizationDeniedError("source cache registration requires a worker principal")

    @classmethod
    def _require_generation(
        cls,
        repository: SourceCacheCleanupRepository,
        *,
        principal: WorkerRepositoryPrincipal,
        worker_id: str,
        generation_id: str,
        session_fence: int,
    ) -> WorkerCacheGenerationRecord:
        generation = repository.get_generation(generation_id)
        if (
            generation is None
            or generation.worker_id != worker_id
            or generation.session_fence != session_fence
            or generation.workspace_id != cls._workspace_scope(principal)
            or generation.state is WorkerCacheGenerationState.Retired
        ):
            raise ConflictError("worker source cache session is no longer current")
        return generation

    @staticmethod
    def _authorize_worker(
        principal: WorkerRepositoryPrincipal,
        worker_id: str,
        *,
        allow_bootstrap: bool = False,
    ) -> None:
        if not (principal.is_managed_worker or principal.is_private_worker):
            raise AuthorizationDeniedError("source cache operation requires a worker principal")
        if principal.worker_id:
            if principal.worker_id != worker_id:
                raise AuthorizationDeniedError(
                    "source cache operation does not match the authenticated worker"
                )
            return
        if not allow_bootstrap:
            raise AuthorizationDeniedError("source cache operation requires a worker session")


__all__ = [
    "SOURCE_CACHE_CLAIM_LEASE_SECONDS",
    "SOURCE_CACHE_RETRY_DELAY_SECONDS",
    "WorkerSourceCacheClaim",
    "WorkerSourceCacheService",
    "WorkerSourceCacheUnavailableError",
]
