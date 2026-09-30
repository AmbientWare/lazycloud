from __future__ import annotations

from api.server.services import ApiServices
from control.service import ControlServices
from shared.deployment_records import (
    DEFAULT_HTTP_CPU,
    DEFAULT_HTTP_KEEP_WARM_SECONDS,
    DEFAULT_HTTP_MEMORY,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DEFAULT_MAX_PENDING_TASKS,
    DeploymentSpec,
    Resources,
)
from shared.deployments import DeploymentKind
from shared.placement import ProductRegion


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
    control = ControlServices.create(
        isolated_services.context,
    )
    stub = control.stubs.get_stub(deployment.stub_id or "")
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


def test_deployment_preserves_region_workers_and_explicit_zero_timeout(
    isolated_services: ApiServices,
) -> None:
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="configured-endpoint",
            kind=DeploymentKind.Endpoint,
            resources=Resources(region=ProductRegion.UsEast, timeout_seconds=0, keep_warm=0),
            metadata={"workers": 3, "max_pending_tasks": 0},
        )
    )
    resource = isolated_services.deployment_resources.resolve(deployment.id, workspace="default")
    assert resource.stub.config.runtime.region is ProductRegion.UsEast
    assert resource.stub.config.runtime.workers == 3
    assert resource.stub.config.runtime.timeout_seconds == 0
    assert resource.stub.config.runtime.keep_warm == 0
    assert resource.stub.config.max_pending_tasks == 0
