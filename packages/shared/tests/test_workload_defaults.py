from __future__ import annotations

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from pydantic import ValidationError
from shared.autoscaling import QueueDepthAutoscaler
from shared.deployment_records import (
    DEFAULT_HTTP_CPU,
    DEFAULT_HTTP_KEEP_WARM_SECONDS,
    DEFAULT_HTTP_MEMORY,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_MAX_PENDING_TASKS,
    DEFAULT_TASK_QUEUE_CPU,
    DEFAULT_TASK_QUEUE_KEEP_WARM_SECONDS,
    DEFAULT_TASK_QUEUE_MEMORY,
    DEFAULT_TASK_QUEUE_RETRIES,
    DEFAULT_TASK_QUEUE_TIMEOUT_SECONDS,
    DeploymentSpec,
    Resources,
)
from shared.deployments import DeploymentKind
from shared.http.deployments import DeploymentResourcesResponse
from shared.http.gateway import Autoscaler, GetOrCreateStubRequest
from shared.http.stubs import StubRuntimeConfigResponse


def test_deployment_concurrency_is_positive_at_public_http_boundaries() -> None:
    omitted = DeploymentSpec(name="omitted")
    request = GetOrCreateStubRequest(name="omitted")
    autoscaler = QueueDepthAutoscaler()

    assert omitted.resources.concurrency == 1
    assert request.concurrent_requests == 1
    assert autoscaler.min_containers == 0
    assert autoscaler.max_containers == 1
    assert autoscaler.tasks_per_container == 1

    with pytest.raises(ValidationError, match="concurrency must be greater than zero"):
        DeploymentSpec(name="invalid", resources=Resources(concurrency=0))
    with pytest.raises(ValidationError, match="greater than 0"):
        GetOrCreateStubRequest(name="invalid", concurrent_requests=0)
    with pytest.raises(ValidationError, match="greater than 0"):
        DeploymentResourcesResponse(concurrency=0)
    with pytest.raises(ValidationError, match="greater than 0"):
        StubRuntimeConfigResponse(concurrency=0)
    with pytest.raises(ValidationError, match="only supported for pod"):
        GetOrCreateStubRequest(name="invalid", keep_warm_seconds=-1)
    with pytest.raises(ValidationError, match="requires max_containers"):
        DeploymentSpec(
            name="contradictory",
            kind=DeploymentKind.Pod,
            resources=Resources(keep_warm=-1),
            metadata={"autoscaler": {"max_containers": 0}},
        )
    with pytest.raises(ValidationError, match="requires max_containers"):
        GetOrCreateStubRequest(
            name="contradictory",
            stub_type=DeploymentKind.Pod.value,
            keep_warm_seconds=-1,
            autoscaler=Autoscaler(max_containers=0),
        )


@pytest.mark.parametrize(
    ("kind", "cpu", "memory", "timeout", "keep_warm", "retry_count"),
    [
        (
            DeploymentKind.Endpoint,
            DEFAULT_HTTP_CPU,
            DEFAULT_HTTP_MEMORY,
            DEFAULT_HTTP_TIMEOUT_SECONDS,
            DEFAULT_HTTP_KEEP_WARM_SECONDS,
            None,
        ),
        (
            DeploymentKind.TaskQueue,
            DEFAULT_TASK_QUEUE_CPU,
            DEFAULT_TASK_QUEUE_MEMORY,
            DEFAULT_TASK_QUEUE_TIMEOUT_SECONDS,
            DEFAULT_TASK_QUEUE_KEEP_WARM_SECONDS,
            DEFAULT_TASK_QUEUE_RETRIES,
        ),
    ],
)
def test_raw_deployment_persists_canonical_runtime_defaults(
    isolated_services: ApiServices,
    kind: DeploymentKind,
    cpu: int,
    memory: int,
    timeout: int,
    keep_warm: int,
    retry_count: int | None,
) -> None:
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name=f"raw-{kind.value}",
            kind=kind,
            route="/raw" if kind is DeploymentKind.Endpoint else None,
        )
    )
    control = ControlPlaneService(isolated_services.context)
    stub = control.get_stub(deployment.stub_id or "")
    runtime = stub.config.runtime

    assert deployment.spec.resources.cpu == cpu
    assert deployment.spec.resources.memory == memory
    assert deployment.spec.resources.timeout_seconds == timeout
    assert deployment.spec.resources.keep_warm == keep_warm
    assert stub.public is False
    assert runtime.cpu == cpu
    assert runtime.memory == memory
    assert runtime.timeout_seconds == timeout
    assert stub.config.max_pending_tasks == DEFAULT_MAX_PENDING_TASKS
    if retry_count is not None:
        assert deployment.spec.retry_policy is not None
        assert deployment.spec.retry_policy.retry_count == retry_count
