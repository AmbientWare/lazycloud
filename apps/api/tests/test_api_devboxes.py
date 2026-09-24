from __future__ import annotations

from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from fastapi.testclient import TestClient
from identity.auth import TokenIssuer
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind, DevboxState, PodRole
from shared.disks import DiskStatus
from shared.http.deployments import DeploymentDetailResponse, DeploymentListResponse
from shared.http.disks import DiskListResponse
from shared.ssh import ssh_host_alias
from storage.disks import get_or_create_disks
from tests.workspaces import on_team_plan, owned_workspace, workspace_owner_user_id

GIB = 1024**3


def test_a_devbox_detail_carries_its_role_connection_and_disk(
    isolated_services: ApiServices,
) -> None:
    workspace = owned_workspace(ControlPlaneService(isolated_services.context), "devbox-owner")
    devbox = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="box",
            kind=DeploymentKind.Pod,
            role=PodRole.Devbox,
            root_disk_bytes=20 * GIB,
            metadata={"app": "dev"},
        ),
        workspace=workspace.id,
    )
    service = isolated_services.deployments.deploy(
        DeploymentSpec(name="web", kind=DeploymentKind.Pod, metadata={"app": "dev"}),
        workspace=workspace.id,
    )
    assert devbox.stub_id is not None
    on_team_plan(isolated_services.database, workspace.id)
    get_or_create_disks(
        isolated_services.context.database,
        list(devbox.spec.disks),
        workspace_id=workspace.id,
        stub_id=devbox.stub_id,
    )
    issuer = TokenIssuer(isolated_services.context)
    with isolated_services.context.database.session() as session:
        token, _ = issuer.issue_for_user(
            session,
            "cli",
            user_id=workspace_owner_user_id(isolated_services.context, workspace.id),
        )
    issuer.committed()
    headers = {"Authorization": f"Bearer {token}"}
    params = {"workspace": workspace.id}

    with TestClient(create_app(isolated_services)) as client:
        devbox_detail = client.get(
            f"/api/v1/deployments/{devbox.id}", params=params, headers=headers
        )
        service_detail = client.get(
            f"/api/v1/deployments/{service.id}", params=params, headers=headers
        )
        listed = client.get("/api/v1/deployments", params=params, headers=headers)
        disks = client.get("/api/v1/disks", params=params, headers=headers)
        isolated_services.deployments.delete(devbox.id, workspace=workspace.id)
        after_delete = client.get("/api/v1/disks", params=params, headers=headers)

    detail = DeploymentDetailResponse.model_validate_json(devbox_detail.content)
    assert detail.role is PodRole.Devbox
    assert detail.devbox is not None
    assert detail.devbox.ssh_command == "lazycloud ssh box"
    assert detail.devbox.ssh_host == ssh_host_alias(workspace.name, "dev", "box")
    assert detail.devbox.state is DevboxState.Stopped
    assert detail.devbox.open_connections == 0
    assert detail.devbox.idle_deadline is None
    assert detail.devbox.disk is not None
    assert detail.devbox.disk.name == "box"
    assert detail.devbox.disk.size_bytes == 20 * GIB
    assert detail.devbox.disk.status is DiskStatus.Detached

    other = DeploymentDetailResponse.model_validate_json(service_detail.content)
    assert other.role is PodRole.Service
    assert other.devbox is None

    roles = {
        item.name: item.role
        for item in DeploymentListResponse.model_validate_json(listed.content).data
    }
    assert roles == {"box": PodRole.Devbox, "web": PodRole.Service}

    [disk] = DiskListResponse.model_validate_json(disks.content).data
    assert disk.workload is not None
    assert (disk.workload.app_name, disk.workload.name, disk.workload.role) == (
        "dev",
        "box",
        PodRole.Devbox,
    )
    assert DiskListResponse.model_validate_json(after_delete.content).data[0].workload is None
