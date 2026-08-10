from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from database.repositories.images import CheckpointRepository
from shared.checkpoints import (
    CheckpointRecord,
    CheckpointStatus,
    checkpoint_recent_stub_key,
)
from tests.service_fixtures import owned_workspace


def test_checkpoint_repository_lifecycle_uses_database(isolated_services: ApiServices) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "workspace-1")
    other_workspace = owned_workspace(control, "workspace-2")
    stub = control.create_stub("stub-1", workspace=workspace.id, kind=StubKind.Function)
    other_stub = control.create_stub(
        "stub-2",
        workspace=other_workspace.id,
        kind=StubKind.Function,
    )
    session_context = isolated_services.context.database.session()
    session = session_context.__enter__()
    repo = CheckpointRepository(session)
    first = repo.create(
        CheckpointRecord(
            checkpoint_id="checkpoint-1",
            container_ip="10.0.0.10",
            status=CheckpointStatus.Pending,
            remote_key="checkpoints/checkpoint-1.tar",
            workspace_id=workspace.id,
            stub_id=stub.id,
            stub_type="function",
            exposed_ports=[8080],
            cache_hash="cache-hash-1",
            cache_size_bytes=1024,
            origin_key="origin/key",
            locality="den",
            accelerator="L4",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    second = repo.create(
        CheckpointRecord(
            checkpoint_id="checkpoint-2",
            workspace_id=workspace.id,
            stub_id=stub.id,
            status=CheckpointStatus.Available,
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
    )
    other_workspace_checkpoint = repo.create(
        CheckpointRecord(
            checkpoint_id="checkpoint-3",
            workspace_id=other_workspace.id,
            stub_id=other_stub.id,
            created_at=datetime(2026, 1, 3, tzinfo=UTC),
        )
    )

    restored_at = datetime(2026, 1, 4, tzinfo=UTC)
    updated = repo.update(
        first.checkpoint_id,
        status=CheckpointStatus.Available,
        last_restored_at=restored_at,
    )
    loaded_first = repo.get(first.checkpoint_id, workspace_id=workspace.id)
    assert loaded_first is not None

    assert loaded_first.status is CheckpointStatus.Available
    assert updated.last_restored_at == restored_at
    assert repo.latest_for_stub(stub.id).checkpoint_id == second.checkpoint_id
    assert [item.checkpoint_id for item in repo.list(workspace_id=workspace.id)] == [
        second.checkpoint_id,
        first.checkpoint_id,
    ]
    assert repo.list(workspace_id=other_workspace.id) == [other_workspace_checkpoint]
    assert loaded_first.cache_hash == "cache-hash-1"

    assert repo.get_across_workspaces("missing") is None
    with pytest.raises(KeyError):
        repo.latest_for_stub("missing-stub")
    with pytest.raises(KeyError):
        repo.update("missing", status=CheckpointStatus.Failed)
    session_context.__exit__(None, None, None)


def test_checkpoint_repository_requires_durable_expiration_before_pruning(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "workspace-1")
    other_workspace = owned_workspace(control, "workspace-2")
    active_stub = control.create_stub(
        "stub-active",
        workspace=workspace.id,
        kind=StubKind.Function,
    )
    stale_stub = control.create_stub(
        "stub-stale",
        workspace=workspace.id,
        kind=StubKind.Function,
    )
    other_stub = control.create_stub(
        "stub-other",
        workspace=other_workspace.id,
        kind=StubKind.Function,
    )
    session_context = isolated_services.context.database.session()
    session = session_context.__enter__()
    repo = CheckpointRepository(session)
    now = datetime(2026, 2, 1, tzinfo=UTC)
    active = repo.create(
        CheckpointRecord(
            checkpoint_id="checkpoint-active",
            workspace_id=workspace.id,
            stub_id=active_stub.id,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            retention_expires_at=now - timedelta(days=1),
        )
    )
    stale = repo.create(
        CheckpointRecord(
            checkpoint_id="checkpoint-stale",
            workspace_id=workspace.id,
            stub_id=stale_stub.id,
            origin_key="checkpoints/checkpoint-stale.tar",
            status=CheckpointStatus.Available,
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
            retention_expires_at=now - timedelta(days=1),
        )
    )
    other_stale = repo.create(
        CheckpointRecord(
            checkpoint_id="checkpoint-other-stale",
            workspace_id=other_workspace.id,
            stub_id=other_stub.id,
            created_at=datetime(2026, 1, 3, tzinfo=UTC),
        )
    )
    future = repo.create(
        CheckpointRecord(
            checkpoint_id="checkpoint-future",
            workspace_id=other_workspace.id,
            stub_id=other_stub.id,
            created_at=datetime(2026, 1, 4, tzinfo=UTC),
            retention_expires_at=now + timedelta(days=1),
        )
    )

    stale_records = repo.list_expired_for_retention(
        active_recent_stub_keys=[checkpoint_recent_stub_key(active.workspace_id, active.stub_id)],
        now=now,
        limit=100,
    )
    assert [item.checkpoint_id for item in stale_records] == [stale.checkpoint_id]

    result = repo.prune([item.checkpoint_id for item in stale_records])
    assert result.count == 1
    assert [item.checkpoint_id for item in result.pruned] == [stale.checkpoint_id]
    assert repo.list_across_workspaces() == [future, other_stale, active]
    deleted = repo.get_across_workspaces(stale.checkpoint_id, include_deleted=True)
    assert deleted is not None
    assert deleted.deleted_at is not None
    assert repo.get_across_workspaces(stale.checkpoint_id) is None

    second_prune = repo.prune([stale.checkpoint_id, "missing"])
    assert second_prune.count == 0
    assert (
        repo.list_expired_for_retention(
            active_recent_stub_keys=[
                checkpoint_recent_stub_key(active.workspace_id, active.stub_id)
            ],
            now=now,
            limit=100,
        )
        == []
    )
    session_context.__exit__(None, None, None)


def test_checkpoint_retention_selects_only_published_or_terminal_records(
    isolated_services: ApiServices,
) -> None:
    workspace = owned_workspace(
        ControlPlaneService(isolated_services.context), "checkpoint-retention-states"
    )
    now = datetime(2026, 2, 1, tzinfo=UTC)
    expired = now - timedelta(seconds=1)
    statuses = (
        CheckpointStatus.Pending,
        CheckpointStatus.Available,
        CheckpointStatus.Failed,
        CheckpointStatus.CheckpointFailed,
        CheckpointStatus.RestoreFailed,
    )
    with isolated_services.context.database.session() as session:
        repository = CheckpointRepository(session)
        for status in statuses:
            repository.create(
                CheckpointRecord(
                    checkpoint_id=f"checkpoint-{status.value}",
                    workspace_id=workspace.id,
                    status=status,
                    retention_expires_at=expired,
                )
            )

        candidates = repository.list_expired_for_retention(
            active_recent_stub_keys=[],
            now=now,
            limit=100,
        )

    assert {candidate.status for candidate in candidates} == {
        CheckpointStatus.Available,
        CheckpointStatus.Failed,
        CheckpointStatus.CheckpointFailed,
        CheckpointStatus.RestoreFailed,
    }
