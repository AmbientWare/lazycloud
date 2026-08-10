from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from compute.source_cache_storage import SourceCacheStorageLifecycleService
from control.service import ControlPlaneService
from database.repositories.source_cache import SourceCacheCleanupRepository
from shared.errors import ConflictError, NotFoundError
from shared.source_cache_cleanup import (
    SourceCacheCleanupCompletionReason,
    SourceCacheCleanupStatus,
    WorkerCacheGenerationState,
    WorkerCacheStorageDestructionEvidence,
    WorkerCacheStorageOwnerKind,
    WorkerCacheStorageOwnerRecord,
)
from tests.service_fixtures import owned_workspace


def test_storage_owner_remains_incomplete_until_explicit_destruction_evidence(
    isolated_services: ApiServices,
) -> None:
    workspace = owned_workspace(
        ControlPlaneService(isolated_services.context), "cache-storage-owner"
    )
    owner = WorkerCacheStorageOwnerRecord(
        kind=WorkerCacheStorageOwnerKind.Machine,
        owner_id="provider-machine-a",
    )
    generation_id = str(uuid4())
    started_at = datetime(2026, 7, 21, 12, tzinfo=UTC)
    with isolated_services.context.database.session() as session:
        repository = SourceCacheCleanupRepository(session)
        repository.register_generation(
            generation_id,
            worker_id="worker-a",
            storage_id=owner.storage_id,
            workspace_id=None,
            now=started_at,
        )
        repository.add_targets(
            workspace_id=workspace.id,
            source_object_ids=[str(uuid4())],
            now=started_at,
        )

    lifecycle = SourceCacheStorageLifecycleService(isolated_services.context)
    before = lifecycle.get(owner)

    assert before.generation_id == generation_id
    assert before.pending_count == 1
    assert before.storage_destroyed_at is None
    assert not before.complete

    destroyed_at = started_at + timedelta(days=30)
    assert lifecycle.record_machine_storage_destroyed(
        owner.owner_id,
        observed_at=destroyed_at,
    )

    after = lifecycle.get(owner, generation_id=generation_id)
    assert after.state is WorkerCacheGenerationState.Retired
    assert after.pending_count == 0
    assert after.completed_count == 1
    assert after.storage_destroyed_at == destroyed_at
    assert after.complete
    with isolated_services.context.database.session() as session:
        [target] = SourceCacheCleanupRepository(session).list_targets(
            generation_ids=[generation_id]
        )
    assert target.status is SourceCacheCleanupStatus.Completed
    assert target.completion_reason is SourceCacheCleanupCompletionReason.StorageDestroyed
    assert target.completed_at == destroyed_at


def test_storage_destruction_evidence_is_fenced_to_exact_owner_generation_and_time(
    isolated_services: ApiServices,
) -> None:
    owner = WorkerCacheStorageOwnerRecord(
        kind=WorkerCacheStorageOwnerKind.Node,
        owner_id="cluster-node-a",
    )
    other_owner = owner.model_copy(update={"owner_id": "cluster-node-b"})
    generation_id = str(uuid4())
    started_at = datetime(2026, 7, 21, 12, tzinfo=UTC)
    with isolated_services.context.database.session() as session:
        SourceCacheCleanupRepository(session).register_generation(
            generation_id,
            worker_id="worker-a",
            storage_id=owner.storage_id,
            workspace_id=None,
            now=started_at,
        )

    lifecycle = SourceCacheStorageLifecycleService(isolated_services.context)
    with pytest.raises(ConflictError, match="evidence predates"):
        lifecycle.record_destroyed(
            WorkerCacheStorageDestructionEvidence(
                owner=owner,
                generation_id=generation_id,
                observed_at=started_at - timedelta(seconds=1),
            )
        )
    with pytest.raises(NotFoundError, match="worker cache storage not found"):
        lifecycle.record_destroyed(
            WorkerCacheStorageDestructionEvidence(
                owner=other_owner,
                generation_id=generation_id,
                observed_at=started_at + timedelta(seconds=1),
            )
        )
    with pytest.raises(NotFoundError, match="worker cache storage not found"):
        lifecycle.record_destroyed(
            WorkerCacheStorageDestructionEvidence(
                owner=owner,
                generation_id=str(uuid4()),
                observed_at=started_at + timedelta(seconds=1),
            )
        )

    current = lifecycle.get(owner, generation_id=generation_id)
    assert current.state is WorkerCacheGenerationState.Initializing
    assert current.storage_destroyed_at is None
    assert not current.complete


def test_machine_without_registered_cache_has_no_cleanup_to_retire(
    isolated_services: ApiServices,
) -> None:
    assert SourceCacheStorageLifecycleService(
        isolated_services.context
    ).record_machine_storage_destroyed(
        "machine-without-cache",
        observed_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
    )
