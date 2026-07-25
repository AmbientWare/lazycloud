from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.deployment_records import DeploymentSpec, Resources
from shared.deployments import DeploymentKind
from shared.http.apps import AppSummaryListResponse
from shared.http.deployments import DeploymentListResponse
from shared.identity import AuthScope, TokenKind


@pytest.fixture
def client_stack() -> Iterator[ExitStack]:
    with ExitStack() as stack:
        yield stack


def test_pod_replica_scaling_is_typed_authorized_and_lifecycle_gated(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    app = isolated_services.apps.create("pod_scaling")
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="web",
            kind=DeploymentKind.Pod,
            resources=Resources(keep_warm=120),
            command=["python", "-m", "http.server", "8080"],
            ports={"8080": 8080},
            metadata={"app": app.name, "app_id": app.id},
        )
    )
    function = isolated_services.deployments.deploy(
        DeploymentSpec(name="calculate", kind=DeploymentKind.Function)
    )
    workspace = ControlPlaneService(isolated_services.context).get_workspace(app.workspace_id)
    auth = AuthService(isolated_services.context)
    writer_token, _ = auth.create_token(
        "pod-scale-writer",
        scopes=[AuthScope.Read.value, AuthScope.Write.value],
        kind=TokenKind.Workspace,
        workspace_id=workspace.id,
    )
    reader_token, _ = auth.create_token(
        "pod-scale-reader",
        scopes=[AuthScope.Read.value],
        kind=TokenKind.Workspace,
        workspace_id=workspace.id,
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    writer_headers = {"Authorization": f"Bearer {writer_token}"}
    reader_headers = {"Authorization": f"Bearer {reader_token}"}

    listed = client.get(
        f"/api/v1/deployments?app_id={app.id}",
        headers=writer_headers,
    )
    assert listed.status_code == 200, listed.text
    listed_pod = DeploymentListResponse.model_validate_json(listed.content).data[0]
    assert listed_pod.id == deployment.id
    assert listed_pod.scaling is not None
    assert listed_pod.scaling.min_replicas == 0
    assert listed_pod.scaling.max_replicas == 1
    assert listed_pod.actions.can_scale is True

    reader_detail = client.get(
        f"/api/v1/deployments/{deployment.id}",
        headers=reader_headers,
    )
    assert reader_detail.status_code == 200, reader_detail.text
    assert reader_detail.json()["scaling"] == {"min_replicas": 0, "max_replicas": 1}
    assert reader_detail.json()["actions"]["can_scale"] is False

    denied = client.post(
        f"/api/v1/deployments/{deployment.id}/scale",
        headers=reader_headers,
        json={"replicas": 2},
    )
    assert denied.status_code == 403, denied.text

    invalid = client.post(
        f"/api/v1/deployments/{deployment.id}/scale",
        headers=writer_headers,
        json={"replicas": -1},
    )
    assert invalid.status_code == 422, invalid.text

    non_pod = client.post(
        f"/api/v1/deployments/{function.id}/scale",
        headers=writer_headers,
        json={"replicas": 2},
    )
    assert non_pod.status_code == 400, non_pod.text
    assert non_pod.json()["detail"] == "only pod deployments can be scaled directly"

    scaled = client.post(
        f"/api/v1/deployments/{deployment.id}/scale",
        headers=writer_headers,
        json={"replicas": 3},
    )
    assert scaled.status_code == 200, scaled.text
    assert scaled.json()["id"] == deployment.id
    assert scaled.json()["scaling"] == {"min_replicas": 3, "max_replicas": 3}
    assert scaled.json()["actions"]["can_scale"] is True

    scaled_to_zero = client.post(
        f"/api/v1/deployments/{deployment.id}/scale",
        headers=writer_headers,
        json={"replicas": 0},
    )
    assert scaled_to_zero.status_code == 200, scaled_to_zero.text
    assert scaled_to_zero.json()["scaling"] == {"min_replicas": 0, "max_replicas": 0}

    summary = client.get("/api/v1/apps/summaries", headers=writer_headers)
    assert summary.status_code == 200, summary.text
    summaries = AppSummaryListResponse.model_validate_json(summary.content)
    app_summary = next(item for item in summaries.items if item.app.id == app.id)
    assert app_summary.latest_deployment is not None
    assert app_summary.latest_deployment.scaling is not None
    assert app_summary.latest_deployment.scaling.min_replicas == 0
    assert app_summary.latest_deployment.scaling.max_replicas == 0
    assert app_summary.latest_deployment.actions.can_scale is True

    stopped = client.post(
        f"/api/v1/deployments/{deployment.id}/stop",
        headers=writer_headers,
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["actions"]["can_scale"] is False

    inactive = client.post(
        f"/api/v1/deployments/{deployment.id}/scale",
        headers=writer_headers,
        json={"replicas": 1},
    )
    assert inactive.status_code == 409, inactive.text
    assert inactive.json()["detail"] == "cannot scale inactive deployment: web"

    pod_stub = next(
        stub
        for stub in ControlPlaneService(isolated_services.context).list_stubs(
            workspace=workspace.id
        )
        if stub.deployment_id == deployment.id and stub.kind is StubKind.Pod
    )
    assert pod_stub.config.runtime.keep_warm == 120
    assert pod_stub.config.autoscaler.min_containers == 0
    assert pod_stub.config.autoscaler.max_containers == 0


def test_pod_scale_rejects_incompatible_checkpoint_before_mutation(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    app = isolated_services.apps.create("checkpoint_scaling")
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="checkpoint-invalid-multi-gpu",
            kind=DeploymentKind.Pod,
            resources=Resources(gpu="nvidia", gpu_count=2, keep_warm=120),
            command=["python", "-m", "http.server", "8080"],
            ports={"8080": 8080},
            metadata={
                "app": app.name,
                "app_id": app.id,
                "checkpoint_enabled": True,
                "checkpoint_readiness_path": "/ready",
                "checkpoint_readiness_port": 8080,
            },
        )
    )
    control_plane = ControlPlaneService(isolated_services.context)
    workspace = control_plane.get_workspace(app.workspace_id)
    token, _ = AuthService(isolated_services.context).create_token(
        "checkpoint-scale-writer",
        scopes=[AuthScope.Read.value, AuthScope.Write.value],
        kind=TokenKind.Workspace,
        workspace_id=workspace.id,
    )

    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    response = client.post(
        f"/api/v1/deployments/{deployment.id}/scale",
        headers={"Authorization": f"Bearer {token}"},
        json={"replicas": 1},
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "checkpointing does not support more than one GPU"
    pod_stub = next(
        stub
        for stub in control_plane.list_stubs(workspace=workspace.id)
        if stub.deployment_id == deployment.id and stub.kind is StubKind.Pod
    )
    assert pod_stub.config.autoscaler.min_containers == 0
    assert pod_stub.config.autoscaler.max_containers == 1
    assert not any(
        container.stub_id == pod_stub.id for container in isolated_services.containers.list()
    )
