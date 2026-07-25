from __future__ import annotations

import math
from datetime import timedelta
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.orchestration import ContainerRepository
from operations.management import ManagementService
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployment_records import DeploymentSpec
from shared.errors import InvalidInputError
from shared.tasks import TaskStatus
from shared.timestamps import utc_now


def _seed_stub(services: ApiServices):
    deployment = services.deployments.deploy(
        DeploymentSpec(name="latency-demo", handler="pkg.module:handler")
    )
    stub = next(
        item
        for item in ControlPlaneService(services.context).list_stubs()
        if item.deployment_id == deployment.id
    )
    return deployment, stub


def _seed_finished_task(
    services: ApiServices,
    *,
    stub_id: str,
    deployment_id: str,
    runtime_ms: float,
    status: TaskStatus = TaskStatus.Complete,
) -> None:
    task = services.tasks.create(
        "latency-run",
        workspace_id=None,
        stub_id=stub_id,
        deployment_id=deployment_id,
        command=[],
    )
    task.status = status
    task.started_at = utc_now() - timedelta(milliseconds=runtime_ms)
    task.finished_at = task.started_at + timedelta(milliseconds=runtime_ms)
    services.tasks.save(task)


def _seed_container(services: ApiServices, *, workspace_id: str, stub_id: str) -> None:
    container = ContainerRecord(
        id=str(uuid4()),
        name="latency-container",
        image="img-latency",
        command=["python3.12", "-m", "runner.function"],
        workspace_id=workspace_id,
        stub_id=stub_id,
        status=ContainerStatus.Running,
    )
    with services.context.database.session() as session:
        ContainerRepository(session).upsert(container)


def test_task_latency_percentiles_and_cold_starts(isolated_services: ApiServices) -> None:
    deployment, stub = _seed_stub(isolated_services)
    for runtime_ms in (100, 200, 300, 400):
        _seed_finished_task(
            isolated_services,
            stub_id=stub.id,
            deployment_id=deployment.id,
            runtime_ms=runtime_ms,
            status=TaskStatus.Failed if runtime_ms == 400 else TaskStatus.Complete,
        )
    _seed_container(isolated_services, workspace_id=stub.workspace_id, stub_id=stub.id)
    _seed_container(isolated_services, workspace_id=stub.workspace_id, stub_id=stub.id)

    series = ManagementService(isolated_services).task_latency_timeseries(
        "default",
        stub_ids=(stub.id,),
        window_seconds=3600,
    )
    assert series.stub_ids == (stub.id,)
    assert series.window_seconds == 3600
    assert sum(bucket.count for bucket in series.buckets) == 4
    assert sum(bucket.cold_starts for bucket in series.buckets) == 2
    assert sum(bucket.status_counts.get(TaskStatus.Failed, 0) for bucket in series.buckets) == 1
    populated = [bucket for bucket in series.buckets if bucket.count]
    assert len(populated) == 1
    bucket = populated[0]
    assert bucket.p50_ms is not None and math.isclose(bucket.p50_ms, 250, abs_tol=1)
    assert bucket.p95_ms is not None and math.isclose(bucket.p95_ms, 385, abs_tol=1)
    assert bucket.p95_ms is not None and bucket.p50_ms is not None
    assert bucket.p95_ms > bucket.p50_ms


def test_task_latency_scopes_by_stub_and_deployment(isolated_services: ApiServices) -> None:
    deployment, stub = _seed_stub(isolated_services)
    _seed_finished_task(
        isolated_services,
        stub_id=stub.id,
        deployment_id=deployment.id,
        runtime_ms=500,
    )
    other_deployment, other_stub = _seed_stub(isolated_services)
    _seed_finished_task(
        isolated_services,
        stub_id=other_stub.id,
        deployment_id=other_deployment.id,
        runtime_ms=9_000,
    )

    management = ManagementService(isolated_services)
    scoped = management.task_latency_timeseries("default", stub_ids=(stub.id,))
    assert sum(bucket.count for bucket in scoped.buckets) == 1
    assert scoped.buckets[0].p50_ms is not None
    assert math.isclose(scoped.buckets[0].p50_ms, 500, abs_tol=1)

    by_deployment = management.task_latency_timeseries(
        "default",
        stub_ids=(stub.id, other_stub.id),
        deployment_id=other_deployment.id,
    )
    assert sum(bucket.count for bucket in by_deployment.buckets) == 1
    assert by_deployment.buckets[0].p50_ms is not None
    assert math.isclose(by_deployment.buckets[0].p50_ms, 9_000, abs_tol=1)

    with pytest.raises(InvalidInputError):
        management.task_latency_timeseries("default", stub_ids=())
    with pytest.raises(InvalidInputError):
        management.task_latency_timeseries("default", stub_ids=(stub.id,), window_seconds=0)
