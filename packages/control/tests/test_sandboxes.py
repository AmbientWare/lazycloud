from datetime import timedelta
from uuid import uuid4

import pytest
from control.service import ControlServices
from database.context import ServiceContext
from database.records.apps import StubKind, StubRecord
from database.repositories.apps import StubRepository
from database.repositories.orchestration import ContainerRepository
from shared.containers import ContainerRecord, ContainerStatus
from shared.http.pods import SandboxDashboardStatus
from shared.timestamps import utc_now
from shared.workload_config import StubConfig
from tests.workspaces import owned_workspace


def test_sandbox_statistics_cover_history_and_instances_but_lists_stay_bounded(
    committed_service_context: ServiceContext,
) -> None:
    control = ControlServices.create(committed_service_context)
    workspace = control.workspaces.get_workspace()
    peer = owned_workspace(control, "peer")
    now = utc_now()
    old = now - timedelta(days=3)
    with committed_service_context.database.session() as session:
        stubs = StubRepository(session)
        for index in range(205):
            stubs.upsert(
                StubRecord(
                    id=str(uuid4()),
                    workspace_id=workspace.id,
                    name=f"old-{index}",
                    kind=StubKind.Sandbox,
                    created_at=old,
                )
            )
        recent = stubs.upsert(
            StubRecord(
                id=str(uuid4()),
                workspace_id=workspace.id,
                name="restarted",
                kind=StubKind.Sandbox,
                created_at=old,
                config=StubConfig(status="running"),
            )
        )
        stubs.upsert(
            StubRecord(id=str(uuid4()), workspace_id=peer.id, name="peer", kind=StubKind.Sandbox)
        )
        for index, status in enumerate(
            (ContainerStatus.Exited, ContainerStatus.Running, ContainerStatus.Stopped)
        ):
            ContainerRepository(session).upsert(
                ContainerRecord(
                    id=str(uuid4()),
                    name=f"run-{index}",
                    image="image",
                    command=[],
                    workspace_id=workspace.id,
                    stub_id=recent.id,
                    status=status,
                    created_at=old if index == 0 else now - timedelta(hours=1),
                    started_at=old if index == 0 else now - timedelta(minutes=30 - index),
                )
            )
    stats = control.sandboxes.sandbox_stats(workspace=workspace.id)
    assert stats.total_created == 208
    assert stats.concurrent == 1
    assert stats.status_counts[SandboxDashboardStatus.Stopped] == 207
    assert stats.rate_per_second == pytest.approx(2 / 86400)
    assert [bucket.count for bucket in stats.created_buckets] == [206, 2]
    rows = control.sandboxes.list_sandbox_rows(workspace=workspace.id, limit=500).data
    assert len(rows) == 200
    # The latest instance ended even though an older instance and the config say running.
    timeline = control.sandboxes.sandbox_timeline(recent.id, workspace=workspace.id)
    assert timeline.status is SandboxDashboardStatus.Stopped
    assert control.sandboxes.sandbox_stats(workspace=peer.id).total_created == 1
    assert (
        control.sandboxes.sandbox_stats(workspace=workspace.id, app_id=str(uuid4())).total_created
        == 0
    )
