from __future__ import annotations

from uuid import uuid4

import pytest
from control.service import ControlPlaneService
from database.context import ServiceContext
from database.repositories.source_cache import SourceCacheCleanupRepository
from shared.errors import ConflictError
from shared.identity import TokenKind
from shared.source_cache_cleanup import SourceCacheCleanupStatus
from shared.timestamps import utc_now
from tests.workspaces import owned_workspace
from worker.repository_payloads import WorkerRepositoryPrincipal
from worker_repository.source_cache import WorkerSourceCacheService


def test_private_worker_cannot_resolve_another_workspace_cache_claim(
    service_context: ServiceContext,
) -> None:
    control = ControlPlaneService(service_context)
    owner_workspace = owned_workspace(control, "source-cache-owner")
    other_workspace = owned_workspace(control, "source-cache-other")
    worker_id = "private-worker"
    owner = WorkerRepositoryPrincipal(
        workspace_id=owner_workspace.id,
        worker_id=worker_id,
        token_kind=TokenKind.WorkerPrivate,
    )
    other = owner.model_copy(update={"workspace_id": other_workspace.id})
    service = WorkerSourceCacheService(service_context)
    generation = service.register(
        principal=owner,
        worker_id=worker_id,
        generation_id=str(uuid4()),
        # Production always sends the owner-qualified form the worker builds.
        storage_id="machine:private-cache-machine",
    )
    source_object_id = str(uuid4())
    with service_context.database.session() as session:
        SourceCacheCleanupRepository(session).add_targets(
            workspace_id=owner_workspace.id,
            source_object_ids=[source_object_id],
            now=utc_now(),
        )
    [target] = service.claim(
        principal=owner,
        worker_id=worker_id,
        generation_id=generation.id,
        session_fence=generation.session_fence,
        limit=1,
    ).targets
    assert target.claim_token is not None

    with pytest.raises(ConflictError, match="session is no longer current"):
        service.complete(
            principal=other,
            worker_id=worker_id,
            generation_id=generation.id,
            session_fence=generation.session_fence,
            target_id=target.id,
            claim_token=target.claim_token,
        )
    with pytest.raises(ConflictError, match="session is no longer current"):
        service.fail(
            principal=other,
            worker_id=worker_id,
            generation_id=generation.id,
            session_fence=generation.session_fence,
            target_id=target.id,
            claim_token=target.claim_token,
        )

    with service_context.database.session() as session:
        [current] = SourceCacheCleanupRepository(session).list_targets(
            generation_ids=[generation.id]
        )
    assert current.status is SourceCacheCleanupStatus.Claimed
