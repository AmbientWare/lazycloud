from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.identity import WorkspaceRepository
from database.repositories.source_cache import SourceCacheCleanupRepository
from shared.errors import NotFoundError
from worker_repository.source_cache_status import SourceCacheCleanupStatusService


def test_source_cache_cleanup_status_is_bounded_and_resolves_deleted_workspace(
    isolated_services: ApiServices,
) -> None:
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("cleanup-status")
    started_at = datetime(2026, 7, 21, 12, tzinfo=UTC)
    with isolated_services.context.database.session() as session:
        repository = SourceCacheCleanupRepository(session)
        repository.register_generation(
            str(uuid4()),
            worker_id="worker-a",
            storage_id="physical-cache-a",
            workspace_id=None,
            now=started_at,
        )
        repository.add_targets(
            workspace_id=workspace.id,
            source_object_ids=[str(uuid4()), str(uuid4())],
            now=started_at,
        )
        workspaces = WorkspaceRepository(session)
        deleting = workspaces.mark_deleting(workspace)
        workspaces.tombstone(deleting)

    result = SourceCacheCleanupStatusService(isolated_services.context).get(
        workspace.name,
        now=started_at + timedelta(seconds=73, microseconds=900_000),
    )

    assert result.workspace_id == workspace.id
    assert result.pending_count == 2
    assert result.claimed_count == 0
    assert result.completed_count == 0
    assert result.generations_pending == 1
    assert result.oldest_pending_age_seconds == 73
    assert not result.complete
    assert not hasattr(result, "oldest_pending_at")
    assert not hasattr(result, "source_object_ids")
    assert not hasattr(result, "cache_paths")


def test_source_cache_cleanup_status_clamps_future_clock_and_reports_missing(
    isolated_services: ApiServices,
) -> None:
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace("clock-status")
    started_at = datetime(2026, 7, 21, 12, tzinfo=UTC)
    with isolated_services.context.database.session() as session:
        repository = SourceCacheCleanupRepository(session)
        repository.register_generation(
            str(uuid4()),
            worker_id="worker-a",
            storage_id="physical-cache-a",
            workspace_id=None,
            now=started_at,
        )
        repository.add_targets(
            workspace_id=workspace.id,
            source_object_ids=[str(uuid4())],
            now=started_at,
        )

    result = SourceCacheCleanupStatusService(isolated_services.context).get(
        workspace.id,
        now=started_at - timedelta(seconds=1),
    )

    assert result.oldest_pending_age_seconds == 0
    with pytest.raises(NotFoundError, match="workspace not found"):
        SourceCacheCleanupStatusService(isolated_services.context).get("missing-workspace")
