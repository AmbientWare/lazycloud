from __future__ import annotations

from datetime import timedelta

from api.server.services import ApiServices
from fastapi.testclient import TestClient
from shared.http.tasks import (
    TaskMetricsSummaryResponse,
    TaskPageResponse,
    TaskTimeWindowBucketListResponse,
)
from shared.identity import WorkspaceRecord
from shared.tasks import Task, TaskStatus
from shared.timestamps import utc_now


def _seed_task(
    services: ApiServices,
    name: str,
    *,
    workspace_id: str,
    status: TaskStatus,
    app_id: str | None = None,
    runtime_ms: float | None = None,
    startup_ms: float = 1_000,
) -> Task:
    # Creation time is the row's own, not a back-dated one. The stored column and
    # the payload are written together and every aggregate reads the column, so a
    # task seeded with only the payload moved is a row production cannot produce
    # and makes startup measure the gap between the two rather than the task's.
    task = services.tasks.create(name, workspace_id=workspace_id, app_id=app_id, command=[])
    task.status = status
    if runtime_ms is not None:
        task.started_at = task.created_at + timedelta(milliseconds=startup_ms)
        task.finished_at = task.started_at + timedelta(milliseconds=runtime_ms)
    return services.tasks.save(task)


def test_task_metrics_api_exposes_percentiles_and_app_filter(
    api_runtime: tuple[ApiServices, TestClient],
    api_workspace: WorkspaceRecord,
    api_client: TestClient,
) -> None:
    services, _ = api_runtime
    app_id = services.apps.create("metrics_api_app", workspace=api_workspace.id).id
    _seed_task(
        services,
        "run",
        status=TaskStatus.Complete,
        workspace_id=api_workspace.id,
        app_id=app_id,
        runtime_ms=500,
    )
    _seed_task(services, "unscoped", status=TaskStatus.Failed, workspace_id=api_workspace.id)

    window = {
        "started_at": int((utc_now() - timedelta(hours=1)).timestamp()),
        "ended_at": int((utc_now() + timedelta(minutes=1)).timestamp()),
    }

    response = api_client.get("/api/v1/tasks/metrics", params=window)
    assert response.status_code == 200
    body = TaskMetricsSummaryResponse.model_validate_json(response.content)
    assert body.total == 2
    assert body.failure_rate == 0.5
    assert body.runtime_ms_p50 == 500
    assert body.runtime_ms_p95 == 500
    assert body.startup_ms_p50 == 1_000

    scoped = api_client.get(
        "/api/v1/tasks/metrics",
        params={**window, "app_id": app_id},
    )
    assert scoped.status_code == 200
    scoped_body = TaskMetricsSummaryResponse.model_validate_json(scoped.content)
    assert scoped_body.total == 1
    assert scoped_body.failure_rate == 0.0

    buckets = api_client.get(
        "/api/v1/tasks/aggregate-by-time-window",
        params={"window_seconds": 3600, "app_id": app_id},
    )
    assert buckets.status_code == 200
    bucket_page = TaskTimeWindowBucketListResponse.model_validate_json(buckets.content)
    assert sum(item.count for item in bucket_page.items) == 1

    tasks = api_client.get(
        "/api/v1/tasks",
        params={"app_id": app_id},
    )
    assert tasks.status_code == 200
    listed = TaskPageResponse.model_validate_json(tasks.content).data
    assert [item.name for item in listed] == ["run"]
