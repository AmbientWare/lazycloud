from dataclasses import replace
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from database.records.apps import StubRecord
from database.repositories.apps import StubRepository
from database.repositories.orchestration import ContainerRepository
from identity.auth import AuthorizationDeniedError
from shared.containers import ContainerRecord, ContainerStatus
from shared.disks import DiskMount
from shared.errors import ConflictError
from shared.identity import WorkspaceRecord, WorkspaceStorageConfig
from shared.workload_config import StubConfig
from shared.workspace_storage import WorkspaceStorageGrant
from storage.disks import get_or_create_disks
from tests.workspaces import on_team_plan, owned_workspace
from worker.durable_disk_records import (
    DiskAcquirePayload,
    DiskCollectPayload,
    DiskStorageRequest,
)
from worker.repository_payloads import WorkerRepositoryPrincipal
from worker_repository.credentials import WorkerCredentialService

_ROOT = DiskMount(name="box-root", size_bytes=1024**3)


def test_a_worker_leases_only_the_disks_its_assigned_container_declares(
    isolated_services: ApiServices,
) -> None:
    control = isolated_services.control_plane_service
    workspace = owned_workspace(control, "default")
    other = owned_workspace(control, "other-tenant")
    database = isolated_services.database
    on_team_plan(database, workspace.id)
    on_team_plan(database, other.id)
    stub_id = str(uuid4())
    container_id = str(uuid4())
    with database.session() as session:
        StubRepository(session).upsert(
            StubRecord(
                id=stub_id,
                workspace_id=workspace.id,
                name="box",
                kind=StubKind.Pod,
                config=StubConfig(disks=[_ROOT]),
            )
        )
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name="box",
                image="image",
                command=[],
                workspace_id=workspace.id,
                stub_id=stub_id,
                runtime_worker_id="worker-1",
                status=ContainerStatus.Running,
            )
        )
    [declared, undeclared] = get_or_create_disks(
        database,
        [_ROOT, DiskMount(name="scratch", size_bytes=1024**3)],
        workspace_id=workspace.id,
        stub_id=str(uuid4()),
    )
    [foreign] = get_or_create_disks(database, [_ROOT], workspace_id=other.id, stub_id=str(uuid4()))
    service = isolated_services.worker_repository_service
    assigned = WorkerRepositoryPrincipal(workspace_id=workspace.id, worker_id="worker-1")

    def acquire(disk_id: str, principal: WorkerRepositoryPrincipal) -> str:
        return service.acquire_disk(
            DiskAcquirePayload(container_id=container_id, disk_id=disk_id),
            principal=principal,
        ).lease_token

    with pytest.raises(AuthorizationDeniedError, match="another worker"):
        acquire(declared.record.id, assigned.model_copy(update={"worker_id": "worker-2"}))
    with pytest.raises(AuthorizationDeniedError, match="does not declare"):
        acquire(undeclared.record.id, assigned)
    with pytest.raises(AuthorizationDeniedError, match="workspace"):
        acquire(foreign.record.id, assigned)
    token = acquire(declared.record.id, assigned)

    def collect(disk_id: str, principal: WorkerRepositoryPrincipal) -> None:
        service.collect_disk(
            DiskCollectPayload(
                container_id=container_id,
                disk_id=disk_id,
                lease_token=token,
                generation=1,
                stored_bytes_removed=0,
            ),
            principal=principal,
        )

    with pytest.raises(AuthorizationDeniedError, match="another worker"):
        collect(declared.record.id, assigned.model_copy(update={"worker_id": "worker-2"}))
    with pytest.raises(AuthorizationDeniedError, match="does not declare"):
        collect(undeclared.record.id, assigned)

    # Once stopped, the container has no scheduler state to vend credentials on,
    # and its release still has to publish. The lease is the authority.
    ControlPlaneService(isolated_services.context).set_workspace_storage(
        workspace.id, WorkspaceStorageConfig(backend="s3", bucket="workspace-bucket")
    )
    with database.session() as session:
        stopped = ContainerRepository(session).lock_across_workspaces(container_id)
        assert stopped is not None
        stopped.status = ContainerStatus.Stopped
        ContainerRepository(session).upsert(stopped)
    vending = replace(
        service,
        container_credentials=WorkerCredentialService(
            isolated_services, storage_issuer=_StaticStorageIssuer()
        ),
    )

    def storage(lease_token: str, principal: WorkerRepositoryPrincipal) -> str:
        return vending.disk_storage(
            DiskStorageRequest(
                container_id=container_id, disk_id=declared.record.id, lease_token=lease_token
            ),
            principal=principal,
        ).bucket_name

    assert storage(token, assigned) == "workspace-bucket"
    with pytest.raises(AuthorizationDeniedError, match="another worker"):
        storage(token, assigned.model_copy(update={"worker_id": "worker-2"}))
    with pytest.raises(ConflictError, match="no longer holds"):
        storage("stale-token", assigned)


class _StaticStorageIssuer:
    def issue(self, workspace: WorkspaceRecord) -> WorkspaceStorageGrant:
        return WorkspaceStorageGrant(
            endpoint_url="https://s3.local",
            region="us-test-1",
            bucket_name=workspace.storage.bucket or "",
            prefix=workspace.storage.key_prefix,
            force_path_style=True,
            access_key="workspace-ak",
            secret_key="workspace-sk",
        )

    def retire(self, workspace: WorkspaceRecord) -> None:
        del workspace
