from __future__ import annotations

from api.server.services import ApiServices
from control.service import ControlPlaneService
from shared.deployment_records import (
    DEFAULT_HTTP_CPU,
    DEFAULT_HTTP_KEEP_WARM_SECONDS,
    DEFAULT_HTTP_MEMORY,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_MAX_PENDING_TASKS,
    DeploymentSpec,
)
from shared.deployments import DeploymentKind


def test_raw_deployment_persists_canonical_runtime_defaults(
    isolated_services: ApiServices,
) -> None:
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="raw-endpoint",
            kind=DeploymentKind.Endpoint,
            route="/raw",
        )
    )
    control = ControlPlaneService(isolated_services.context)
    stub = control.get_stub(deployment.stub_id or "")
    runtime = stub.config.runtime

    assert deployment.spec.resources.cpu == DEFAULT_HTTP_CPU
    assert deployment.spec.resources.memory == DEFAULT_HTTP_MEMORY
    assert deployment.spec.resources.timeout_seconds == DEFAULT_HTTP_TIMEOUT_SECONDS
    assert deployment.spec.resources.keep_warm == DEFAULT_HTTP_KEEP_WARM_SECONDS
    assert stub.public is False
    assert runtime.cpu == DEFAULT_HTTP_CPU
    assert runtime.memory == DEFAULT_HTTP_MEMORY
    assert runtime.timeout_seconds == DEFAULT_HTTP_TIMEOUT_SECONDS
    assert stub.config.max_pending_tasks == DEFAULT_MAX_PENDING_TASKS
