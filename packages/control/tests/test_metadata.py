from __future__ import annotations

from api.server.services import ApiServices
from control.service import ControlPlaneService
from pydantic import JsonValue, TypeAdapter
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])
_JSON_OBJECTS_ADAPTER = TypeAdapter(list[dict[str, JsonValue]])


def test_sandbox_deployment_preserves_startup_network_policy(
    isolated_services: ApiServices,
) -> None:
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="restricted-sandbox",
            kind=DeploymentKind.Sandbox,
            metadata={
                "block_network": False,
                "allow_list": ["10.0.0.0/8"],
            },
        )
    )

    stub = ControlPlaneService(isolated_services.context).get_stub(deployment.stub_id or "")

    assert stub.config.runtime.block_network is False
    assert stub.config.runtime.allow_list == ["10.0.0.0/8"]
