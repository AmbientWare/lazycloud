from __future__ import annotations

import json
from dataclasses import replace

import pytest
from api.server.services import ApiServices
from compute.state import RedisComputeStateRepository
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient
from database.repositories.apps import StubRecord
from gateway.stub_config import deployment_spec_from_stub, stub_config
from pydantic import ValidationError
from shared.deployment_records import request_and_limit
from shared.deployments import DeploymentKind
from shared.http.gateway import DeployStubRequest, GetOrCreateStubRequest
from shared.placement import ProductRegion
from shared.workload_config import StubConfig
from tests.redis_fakes import FakeRedis


def test_pod_checkpoint_readiness_is_retained_by_source_and_deployed_stubs(
    isolated_services: ApiServices,
) -> None:
    request = GetOrCreateStubRequest(
        name="checkpoint-pod",
        stub_type=DeploymentKind.Pod.value,
        checkpoint_enabled=True,
        ports=[8080, 9090],
        metadata={
            "checkpoint_readiness_path": "/ready",
            "checkpoint_readiness_port": 8080,
            "checkpoint_readiness_timeout_seconds": 60,
            "checkpoint_readiness_interval_seconds": 0.25,
        },
    )
    normalized = stub_config(request).runtime
    assert normalized.checkpoint_enabled is True
    assert normalized.checkpoint_readiness_path == "/ready"
    assert normalized.checkpoint_readiness_port == 8080
    assert normalized.checkpoint_readiness_timeout_seconds == 60
    assert normalized.checkpoint_readiness_interval_seconds == 0.25

    gateway = replace(
        isolated_services.gateway_service,
        compute_state=RedisComputeStateRepository(
            RedisClient(FakeRedis(), key_prefix="checkpoint-readiness")
        ),
    )
    prepared = gateway.get_or_create_stub(request)
    control = ControlPlaneService(isolated_services.context)
    source = control.get_stub(prepared.stub_id)
    assert source.config.runtime == normalized

    deployed = gateway.deploy_stub(
        DeployStubRequest(
            stub_id=prepared.stub_id,
            name="checkpoint-pod-production",
            workspace="default",
            external_url="https://compute.example",
        )
    )
    assert deployed.invoke_url == f"https://{deployed.stub_id}-8080.compute.example"
    deployment_stub = control.get_stub(deployed.stub_id)
    assert deployment_stub.config.runtime.checkpoint_enabled is True
    assert deployment_stub.config.runtime.checkpoint_readiness_path == "/ready"
    assert deployment_stub.config.runtime.checkpoint_readiness_port == 8080
    assert deployment_stub.config.runtime.checkpoint_readiness_timeout_seconds == 60
    assert deployment_stub.config.runtime.checkpoint_readiness_interval_seconds == 0.25


def test_runtime_prepare_stays_outside_apps_and_deployments_until_publish(
    isolated_services: ApiServices,
) -> None:
    gateway = isolated_services.gateway_service
    request = GetOrCreateStubRequest(
        name="hello",
        app_name="runtime_boundary",
        handler="quickstart:hello",
    )

    prepared = gateway.get_or_create_stub(request)
    repeated = gateway.get_or_create_stub(request)
    peer = gateway.get_or_create_stub(request.model_copy(update={"app_name": "peer_runtime"}))

    control = ControlPlaneService(isolated_services.context)
    runtime = control.get_stub(prepared.stub_id)
    assert repeated.stub_id == runtime.id
    assert peer.stub_id != runtime.id
    assert runtime.app_id is None
    assert runtime.deployment_id is None
    assert runtime.metadata["app"] == "runtime_boundary"
    assert isolated_services.apps.list() == []
    assert control.list_stubs(deployed_only=True) == []

    published = gateway.deploy_stub(
        DeployStubRequest(
            stub_id=runtime.id,
            name="hello",
            workspace="default",
        )
    )

    assert published.app_id is not None
    deployed = control.list_stubs(deployed_only=True)
    assert [stub.id for stub in deployed] == [published.stub_id]
    assert deployed[0].app_id == published.app_id
    assert deployed[0].deployment_id == published.deployment_id


def test_resource_requests_and_placement_survive_storage() -> None:
    """`cpu=(1, 4)` has to reach the deploy that reads the stub back.

    The pair crosses JSON to reach the stub row, so it returns as a list rather
    than the tuple its author wrote. Everything between here and the container
    has to accept both, and the value has to still name a ceiling at the end.
    """
    request = GetOrCreateStubRequest(
        name="paired-resources",
        stub_type=DeploymentKind.Function.value,
        cpu=(1.0, 4.0),
        memory=("1Gi", "2Gi"),
        region=ProductRegion.UsEast,
        availability_zone="use1-az5",
        preemptible=False,
    )

    stored = json.loads(stub_config(request).model_dump_json())
    runtime = StubConfig.model_validate(stored).runtime

    assert request_and_limit(runtime.cpu) == (1.0, 4.0)
    assert request_and_limit(runtime.memory) == ("1Gi", "2Gi")
    assert runtime.region is ProductRegion.UsEast
    assert runtime.availability_zone == "use1-az5"
    assert runtime.preemptible is False

    spec = deployment_spec_from_stub(
        StubRecord(
            id="stub-1",
            workspace_id="workspace-1",
            name="paired-resources",
            config=StubConfig.model_validate(stored),
        ),
        name="paired-resources",
    )
    assert request_and_limit(spec.resources.cpu) == (1.0, 4.0)
    assert request_and_limit(spec.resources.memory) == ("1Gi", "2Gi")
    assert spec.resources.region is ProductRegion.UsEast
    assert spec.resources.availability_zone == "use1-az5"
    assert spec.resources.preemptible is False


def test_a_ceiling_below_its_request_is_refused_at_the_public_boundary() -> None:
    """Refused where it is written, not at container start on a worker."""
    for cpu, memory in (((0.5, 0.25), None), (None, ("2Gi", "1Gi"))):
        with pytest.raises(ValidationError):
            stub_config(
                GetOrCreateStubRequest(
                    name="inverted",
                    stub_type=DeploymentKind.Function.value,
                    cpu=cpu,
                    memory=memory,
                )
            )


def test_a_gpu_the_platform_cannot_schedule_is_refused_at_the_public_boundary() -> None:
    """Refused where it is written, not as `offer_unavailable` hours later.

    `a100` reached here for months. It is a real name that no worker reports and
    no offer carries, so it passed every check and placed nowhere, and what the
    author saw was a deploy that failed for no stated reason.
    """
    with pytest.raises(ValueError) as refusal:
        stub_config(
            GetOrCreateStubRequest(
                name="ambiguous-a100",
                stub_type=DeploymentKind.Function.value,
                gpu=["a100"],
                gpu_count=1,
            )
        )

    assert "A100-40" in str(refusal.value)

    # The order an author wrote survives the boundary intact.
    config = stub_config(
        GetOrCreateStubRequest(
            name="chained",
            stub_type=DeploymentKind.Function.value,
            gpu=["h100", "l4"],
            gpu_count=1,
        )
    )
    assert config.runtime.gpu == ["H100", "L4"]
