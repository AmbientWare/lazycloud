from __future__ import annotations

from contextlib import ExitStack
from uuid import uuid4

from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient
from database.repositories.orchestration import ContainerRepository
from fastapi.testclient import TestClient
from observability.stream_state import RedisEventStreamRepository
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployment_records import DeploymentSpec
from shared.http.observability import (
    ContainerMetricsTimeseriesResponse,
    WorkspaceActivityResponse,
    WorkspaceActivitySeriesKind,
    WorkspaceContainerCountsResponse,
)
from shared.realtime.contracts import (
    ContainerMetricsData,
    ContainerMetricsPayload,
    EventRecordType,
)
from tests.service_fixtures import administrator_credential


def _seed_container(services: ApiServices) -> ContainerRecord:
    deployment = services.deployments.deploy(
        DeploymentSpec(name="metrics-demo", handler="pkg.module:handler")
    )
    stub = next(
        item
        for item in ControlPlaneService(services.context).list_stubs()
        if item.deployment_id == deployment.id
    )
    container = ContainerRecord(
        id=str(uuid4()),
        name="metrics-container",
        image="img-metrics",
        command=["python3.12", "-m", "runner.function"],
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        status=ContainerStatus.Running,
    )
    with services.context.database.session() as session:
        return ContainerRepository(session).upsert(container)


def _publish_sample(
    services: ApiServices,
    container: ContainerRecord,
    *,
    cpu_used: int,
    rss: int,
) -> None:
    payload = ContainerMetricsPayload(
        worker_id="worker-1",
        container_id=container.id,
        workspace_id=container.workspace_id or "",
        stub_id=container.stub_id or "",
        cpu=1000,
        metrics=ContainerMetricsData(
            sample_interval_ms=3000,
            cpu_used=cpu_used,
            cpu_total=1000,
            cpu_pct=cpu_used / 10,
            memory_rss_bytes=rss,
            memory_total_bytes=512 * 1024 * 1024,
            disk_read_bytes=4096,
            disk_write_bytes=8192,
            network_recv_bytes=150_000,
            network_sent_bytes=25_000,
        ),
    )
    RedisEventStreamRepository(services.redis()).append_event(
        EventRecordType.ContainerMetrics,
        payload.model_dump(mode="python"),
    )


def _services_with_redis(
    isolated_services: ApiServices,
    redis: RedisClient,
) -> ApiServices:
    return ApiServices.create(
        isolated_services.database,
        root=isolated_services.root,
        create_schema=False,
        volume_filesystem=isolated_services.volume_filesystem,
        redis_client=redis,
        binary_redis_client=isolated_services.binary_redis_client,
        owns_redis_client=False,
        owns_binary_redis_client=False,
    )


def test_container_metrics_timeseries_empty_and_missing(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    container = _seed_container(isolated_services)

    raw_token, _ = administrator_credential(isolated_services, "metrics-reader")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = {"Authorization": f"Bearer {raw_token}"}

    empty = client.get(
        f"/api/v1/metrics/containers/{container.id}/timeseries",
        headers=headers,
    )
    assert empty.status_code == 200
    assert ContainerMetricsTimeseriesResponse.model_validate_json(empty.content).points == ()

    missing = client.get(
        f"/api/v1/metrics/containers/{uuid4()}/timeseries",
        headers=headers,
    )
    assert missing.status_code == 404


def _seed_activity_container(
    services: ApiServices,
    *,
    workspace_id: str,
    app_id: str | None,
    status: ContainerStatus,
    name: str,
) -> None:
    with services.context.database.session() as session:
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name=name,
                image="img-activity",
                command=["python3.12", "-m", "runner.function"],
                workspace_id=workspace_id,
                app_id=app_id,
                status=status,
            )
        )


def test_workspace_metrics_separate_live_footprint_from_windowed_starts(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = control.get_workspace("default")
    alpha = isolated_services.apps.create("alpha").id
    beta = isolated_services.apps.create("beta").id
    gamma = isolated_services.apps.create("gamma").id
    seeded = (
        (alpha, ContainerStatus.Running, "alpha-0"),
        (alpha, ContainerStatus.Running, "alpha-1"),
        (alpha, ContainerStatus.Running, "alpha-2"),
        (beta, ContainerStatus.Running, "beta-0"),
        (beta, ContainerStatus.Pending, "beta-1"),
        (gamma, ContainerStatus.Running, "gamma-0"),
        # Finished, so it is a start the window counts and not capacity held.
        (None, ContainerStatus.Exited, "loose-0"),
    )
    for app_id, status, name in seeded:
        _seed_activity_container(
            isolated_services,
            workspace_id=workspace.id,
            app_id=app_id,
            status=status,
            name=name,
        )

    raw_token, _ = administrator_credential(isolated_services, "workspace-metrics-reader")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = {"Authorization": f"Bearer {raw_token}"}

    counts = client.get("/api/v1/metrics/workspace/containers", headers=headers)
    assert counts.status_code == 200
    held = WorkspaceContainerCountsResponse.model_validate_json(counts.content)
    assert (held.running, held.pending) == (5, 1)

    activity = client.get(
        "/api/v1/metrics/workspace/activity",
        headers=headers,
        params={"limit": 2},
    )
    assert activity.status_code == 200
    window = WorkspaceActivityResponse.model_validate_json(activity.content)
    assert window.total == len(seeded)
    assert [series.kind for series in window.series] == [
        WorkspaceActivitySeriesKind.App,
        WorkspaceActivitySeriesKind.App,
        WorkspaceActivitySeriesKind.Other,
    ]
    assert [series.app_name for series in window.series[:2]] == ["alpha", "beta"]
    # Every series spans the whole window, so a quiet interval reads as a zero
    # rather than as an interval nobody measured.
    assert {len(series.buckets) for series in window.series} == {24}
    assert [series.buckets[-1].count for series in window.series] == [3, 2, 2]
    assert sum(bucket.count for series in window.series for bucket in series.buckets) == (
        window.total
    )
