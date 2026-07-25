from __future__ import annotations

from dataclasses import replace

from api.server.services import ApiServices
from compute.state import RedisComputeStateRepository
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient
from gateway.stub_config import stub_config
from shared.deployments import DeploymentKind
from shared.http.gateway import DeployStubRequest, GetOrCreateStubRequest
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
    assert deployed.invoke_url == f"https://compute.example/pod/public/{deployed.stub_id}/8080"
    assert f"{deployed.invoke_url}/state" == (
        f"https://compute.example/pod/public/{deployed.stub_id}/8080/state"
    )
    deployment_stub = control.get_stub(deployed.stub_id)
    assert deployment_stub.config.runtime.checkpoint_enabled is True
    assert deployment_stub.config.runtime.checkpoint_readiness_path == "/ready"
    assert deployment_stub.config.runtime.checkpoint_readiness_port == 8080
    assert deployment_stub.config.runtime.checkpoint_readiness_timeout_seconds == 60
    assert deployment_stub.config.runtime.checkpoint_readiness_interval_seconds == 0.25
