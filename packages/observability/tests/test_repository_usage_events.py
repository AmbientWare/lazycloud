from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from api.server.services import ApiServices
from database.repositories.observability import (
    UsageRepository,
    WorkerEventRepository,
)
from database.tables.execution import EventTable
from database.tables.observability import WorkerEventTable
from shared.timestamps import utc_now
from shared.usage import UsageGroupKey, UsageMetric, UsageUnit
from shared.usage_query import UsageQuery
from shared.worker_events import (
    WORKER_POOL_SIZER_DECISION_ACTION,
    WorkerEventRecord,
)
from sqlalchemy import update


def test_event_prune_uses_short_telemetry_and_long_audit_retention(
    isolated_services: ApiServices,
) -> None:
    events = isolated_services.events
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    stale_telemetry = events.emit(
        WORKER_POOL_SIZER_DECISION_ACTION,
        resource_type="worker_pool",
        resource_id="default",
        message="sizer decision",
    )
    stale_audit = events.emit(
        "task.created",
        resource_type="task",
        resource_id="task-old",
        message="ancient task",
        workspace_id=workspace_id,
    )
    recent_audit = events.emit(
        "task.created",
        resource_type="task",
        resource_id="task-new",
        message="recent task",
        workspace_id=workspace_id,
    )
    now = utc_now()
    with isolated_services.context.database.session() as session:
        session.execute(
            update(EventTable)
            .where(EventTable.id == stale_telemetry.id)
            .values(created_at=now - timedelta(days=2))
        )
        session.execute(
            update(EventTable)
            .where(EventTable.id == stale_audit.id)
            .values(created_at=now - timedelta(days=40))
        )

    pruned = events.prune()

    assert pruned == 2
    # Across scopes: the pruned rows were one cluster-scoped and one workspace-scoped.
    remaining = events.list(workspace_id=None)
    assert [item.id for item in remaining] == [recent_audit.id]


def test_worker_event_prune_deletes_aged_rows(isolated_services: ApiServices) -> None:
    worker = isolated_services.compute.register_worker()
    with isolated_services.context.database.session() as session:
        repository = WorkerEventRepository(session)
        stale = repository.append(
            WorkerEventRecord(
                id=str(uuid4()),
                worker_id=worker.id,
                event_type="container.exited",
                resource_id="ctr-old",
            )
        )
        fresh = repository.append(
            WorkerEventRecord(
                id=str(uuid4()),
                worker_id=worker.id,
                event_type="container.exited",
                resource_id="ctr-new",
            )
        )
        session.execute(
            update(WorkerEventTable)
            .where(WorkerEventTable.id == stale.id)
            .values(created_at=utc_now() - timedelta(days=40))
        )

    pruned = isolated_services.worker_events.prune()

    assert pruned == 1
    remaining = isolated_services.worker_events.list()
    assert [item.id for item in remaining] == [fresh.id]


def test_usage_repository_aggregation_groups_by_label_with_metadata_fallback(
    isolated_services: ApiServices,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        usage = UsageRepository(session)
        usage.record(
            workspace_id=workspace_id,
            resource_type="container",
            resource_id="container_1",
            metric=UsageMetric.ContainerDurationMilliseconds,
            quantity=60_000,
            unit=UsageUnit.Milliseconds,
            labels={
                "app_id": "app-a",
                "gpu": "",
                "cpu_millicores": "1000",
                "mem_mb": "0",
                "gpu_count": "0",
            },
        )
        usage.record(
            workspace_id=workspace_id,
            resource_type="container",
            resource_id="container_2",
            metric=UsageMetric.ContainerDurationMilliseconds,
            quantity=30_000,
            unit=UsageUnit.Milliseconds,
            labels={
                "app_id": "app-a",
                "gpu": "T4",
                "cpu_millicores": "1000",
                "mem_mb": "0",
                "gpu_count": "1",
            },
        )
        # Older task-count records carried app_id in metadata only.
        usage.record(
            workspace_id=workspace_id,
            resource_type="function",
            resource_id="stub_1",
            metric=UsageMetric.TaskCount,
            quantity=1,
            unit=UsageUnit.Count,
            labels={"kind": "function"},
            metadata={"app_id": "app-b"},
        )

        by_app = usage.aggregate(
            query=UsageQuery(workspace_id=workspace_id),
            group_by=(UsageGroupKey.App,),
        )
        by_gpu = usage.aggregate(
            query=UsageQuery(workspace_id=workspace_id),
            metric=UsageMetric.ContainerDurationMilliseconds,
            group_by=(UsageGroupKey.Gpu,),
        )

    app_groups = {(row.metric, row.labels["app_id"]): row.quantity for row in by_app}
    assert app_groups[(UsageMetric.ContainerDurationMilliseconds, "app-a")] == 90_000
    assert app_groups[(UsageMetric.TaskCount, "app-b")] == 1

    gpu_groups = {row.labels["gpu"]: row.quantity for row in by_gpu}
    assert gpu_groups[""] == 60_000
    assert gpu_groups["T4"] == 30_000
