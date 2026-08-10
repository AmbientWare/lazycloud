from __future__ import annotations

from contextlib import ExitStack
from datetime import timedelta

from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from shared.http.tasks import (
    TaskMetricsSummaryResponse,
    TaskPageResponse,
    TaskTimeWindowBucketListResponse,
)
from shared.tasks import Task, TaskStatus
from shared.timestamps import utc_now
from tests.service_fixtures import administrator_credential


def _seed_task(
    services: ApiServices,
    name: str,
    *,
    status: TaskStatus,
    app_id: str | None = None,
    runtime_ms: float | None = None,
    startup_ms: float = 1_000,
) -> Task:
    task = services.tasks.create(name, workspace_id=None, app_id=app_id, command=[])
    now = utc_now()
    task.created_at = now - timedelta(minutes=5)
    task.status = status
    if runtime_ms is not None:
        task.started_at = task.created_at + timedelta(milliseconds=startup_ms)
        task.finished_at = task.started_at + timedelta(milliseconds=runtime_ms)
    return services.tasks.save(task)


def test_task_metrics_api_exposes_percentiles_and_app_filter(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    app_id = isolated_services.apps.create("metrics_api_app").id
    _seed_task(
        isolated_services,
        "run",
        status=TaskStatus.Complete,
        app_id=app_id,
        runtime_ms=500,
    )
    _seed_task(isolated_services, "unscoped", status=TaskStatus.Failed)

    raw_token, _ = administrator_credential(isolated_services, "metrics-reader")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = {"Authorization": f"Bearer {raw_token}"}
    window = {
        "started_at": int((utc_now() - timedelta(hours=1)).timestamp()),
        "ended_at": int(utc_now().timestamp()),
    }

    response = client.get("/api/v1/tasks/metrics", headers=headers, params=window)
    assert response.status_code == 200
    body = TaskMetricsSummaryResponse.model_validate_json(response.content)
    assert body.total == 2
    assert body.failure_rate == 0.5
    assert body.runtime_ms_p50 == 500
    assert body.runtime_ms_p95 == 500
    assert body.startup_ms_p50 == 1_000

    scoped = client.get(
        "/api/v1/tasks/metrics",
        headers=headers,
        params={**window, "app_id": app_id},
    )
    assert scoped.status_code == 200
    scoped_body = TaskMetricsSummaryResponse.model_validate_json(scoped.content)
    assert scoped_body.total == 1
    assert scoped_body.failure_rate == 0.0

    buckets = client.get(
        "/api/v1/tasks/aggregate-by-time-window",
        headers=headers,
        params={"window_seconds": 3600, "app_id": app_id},
    )
    assert buckets.status_code == 200
    bucket_page = TaskTimeWindowBucketListResponse.model_validate_json(buckets.content)
    assert sum(item.count for item in bucket_page.items) == 1

    tasks = client.get(
        "/api/v1/tasks",
        headers=headers,
        params={"app_id": app_id},
    )
    assert tasks.status_code == 200
    listed = TaskPageResponse.model_validate_json(tasks.content).data
    assert [item.name for item in listed] == ["run"]
