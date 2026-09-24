from __future__ import annotations

from uuid import uuid4

from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.records.apps import StubPower
from database.repositories.apps import StubRepository
from database.repositories.orchestration import ContainerRepository
from fastapi.testclient import TestClient
from gateway.stub_config import stub_config
from identity.auth import TokenIssuer
from shared.container_requests import StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind, DevboxPhase, DevboxState, PodRole
from shared.disks import DiskStatus
from shared.http.deployments import (
    DeploymentDetailResponse,
    DeploymentListResponse,
    DevboxResponse,
)
from shared.http.disks import DiskListResponse
from shared.http.gateway import GetOrCreateStubRequest
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
    assert service.stub_id is not None
    with isolated_services.context.database.session() as session:
        stubs = StubRepository(session)
        stored = stubs.get(devbox.stub_id, workspace_id=workspace.id)
        assert stored is not None
        # An SDK deploy goes through the gateway, which stores the root disk
        # without its default mount path.
        gateway_config = stub_config(
            GetOrCreateStubRequest(
                name="box",
                stub_type=DeploymentKind.Pod.value,
                role=PodRole.Devbox,
                root_disk_bytes=20 * GIB,
            )
        )
        stubs.upsert(stored.model_copy(update={"config": gateway_config}))
        assert stubs.mounts_root_disk(devbox.stub_id, workspace_id=workspace.id)
        assert not stubs.mounts_root_disk(service.stub_id, workspace_id=workspace.id)

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


def test_start_asks_for_a_devbox_at_once_and_stop_parks_it_with_its_deployment_on(
    isolated_services: ApiServices,
) -> None:
    workspace = owned_workspace(ControlPlaneService(isolated_services.context), "devbox-starter")
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
    stub_id = devbox.stub_id
    assert stub_id is not None
    on_team_plan(isolated_services.database, workspace.id)
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
    container_id = str(uuid4())

    def power() -> StubPower:
        with isolated_services.context.database.session() as session:
            return StubRepository(session).power(stub_id, workspace_id=workspace.id)

    with TestClient(create_app(isolated_services)) as client:
        client.post(f"/api/v1/deployments/{devbox.id}/stop", params=params, headers=headers)
        started = client.post(
            f"/api/v1/deployments/{devbox.id}/devbox/start", params=params, headers=headers
        )
        woken = power()
        with isolated_services.context.database.session() as session:
            ContainerRepository(session).upsert(
                ContainerRecord(
                    id=container_id,
                    name="box",
                    image="devbox",
                    command=["sleep", "infinity"],
                    workspace_id=workspace.id,
                    stub_id=stub_id,
                    status=ContainerStatus.Running,
                )
            )
        stopped = client.post(
            f"/api/v1/deployments/{devbox.id}/devbox/stop", params=params, headers=headers
        )
        detail = client.get(f"/api/v1/deployments/{devbox.id}", params=params, headers=headers)

    assert started.status_code == 200, started.text
    starting = DevboxResponse.model_validate_json(started.content)
    assert (starting.state, starting.phase) == (DevboxState.Starting, DevboxPhase.Queued)
    assert not woken.parked
    assert woken.woken_at is not None

    assert stopped.status_code == 200, stopped.text
    assert DevboxResponse.model_validate_json(stopped.content).state is DevboxState.Stopped
    record = isolated_services.containers.get(container_id)
    assert record.status is ContainerStatus.Stopped
    assert record.termination_reason is StopContainerReason.User
    assert power() == StubPower(parked=True, woken_at=None)
    assert DeploymentDetailResponse.model_validate_json(detail.content).active
