from uuid import uuid4

import pytest
from api.server.services import ApiServices
from database.repositories.orchestration import ContainerRepository
from database.tables.orchestration import ContainerTable
from shared.containers import ContainerRecord, ContainerStatus
from shared.disks import DiskMount, DiskStatus, disk_manifest_key
from shared.errors import ConflictError
from shared.timestamps import utc_now
from storage.disks import DiskPublication, get_or_create_disks


def _container(services: ApiServices, workspace_id: str, worker_id: str) -> str:
    container_id = str(uuid4())
    with services.database.session() as session:
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name="box",
                image="image",
                command=[],
                workspace_id=workspace_id,
                runtime_worker_id=worker_id,
                status=ContainerStatus.Running,
            )
        )
    return container_id


def _publication(
    disk_id: str, container_id: str, token: str, generation: int, parent: int
) -> DiskPublication:
    return DiskPublication(
        disk_id=disk_id,
        container_id=container_id,
        lease_token=token,
        generation=generation,
        parent_generation=parent,
        manifest_key=disk_manifest_key(disk_id, generation),
        manifest_sha256=f"{generation:x}".rjust(64, "0"),
        stored_bytes_added=10,
    )


def test_one_container_writes_a_disk_until_its_worker_releases_it(
    isolated_services: ApiServices,
) -> None:
    disks = isolated_services.disks
    with isolated_services.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    [resolved] = get_or_create_disks(
        isolated_services.database,
        [DiskMount(name="box-root", size_bytes=1024**3)],
        workspace_id=workspace_id,
    )
    disk_id = resolved.record.id
    [again] = get_or_create_disks(
        isolated_services.database,
        [DiskMount(name="box-root", size_bytes=1024**3, mount_path="/data")],
        workspace_id=workspace_id,
    )
    assert again.record.id == disk_id
    first = _container(isolated_services, workspace_id, "worker-a")
    second = _container(isolated_services, workspace_id, "worker-b")

    lease = disks.acquire(disk_id, container_id=first, worker_id="worker-a")
    with pytest.raises(ConflictError, match="held by container"):
        disks.acquire(disk_id, container_id=second, worker_id="worker-b")

    with pytest.raises(ConflictError, match="must be 1"):
        disks.publish(_publication(disk_id, first, lease.lease_token, 2, 0))
    disks.publish(_publication(disk_id, first, lease.lease_token, 1, 0))
    disks.publish(_publication(disk_id, first, lease.lease_token, 2, 1))
    with pytest.raises(ConflictError, match="build on generation 2"):
        disks.publish(_publication(disk_id, first, lease.lease_token, 3, 1))
    disks.publish(_publication(disk_id, first, lease.lease_token, 3, 0))

    # Stopped, but its worker has not released storage: a final publish is still possible.
    with isolated_services.database.session() as session:
        row = session.get(ContainerTable, first)
        assert row is not None
        row.status = ContainerStatus.Stopped.value
    shown = disks.get("box-root", workspace_id=workspace_id)
    assert shown.status is DiskStatus.Detached and not shown.holder_container_id
    with pytest.raises(ConflictError, match="held by container"):
        disks.acquire(disk_id, container_id=second, worker_id="worker-b")

    with isolated_services.database.session() as session:
        row = session.get(ContainerTable, first)
        assert row is not None
        row.storage_released_at = utc_now()
    taken = disks.acquire(disk_id, container_id=second, worker_id="worker-b")
    assert taken.generation == 3
    assert [link.generation for link in taken.chain] == [3]

    with pytest.raises(ConflictError, match="no longer holds"):
        disks.publish(_publication(disk_id, first, lease.lease_token, 4, 3))
    assert not disks.release(disk_id, container_id=first, lease_token=lease.lease_token)
    assert disks.get("box-root", workspace_id=workspace_id).holder_container_id == second

    with pytest.raises(ConflictError, match="before deleting"):
        isolated_services.disk_deletion.request("box-root", workspace_id=workspace_id)
    assert disks.release(disk_id, container_id=second, lease_token=taken.lease_token)
    assert isolated_services.disk_deletion.request("box-root", workspace_id=workspace_id)
    assert not disks.list(workspace_id=workspace_id).data


def test_only_the_holder_collects_under_its_newest_self_contained_generation(
    isolated_services: ApiServices,
) -> None:
    disks = isolated_services.disks
    with isolated_services.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    [resolved] = get_or_create_disks(
        isolated_services.database,
        [DiskMount(name="box-data", size_bytes=1024**3)],
        workspace_id=workspace_id,
    )
    disk_id = resolved.record.id
    holder = _container(isolated_services, workspace_id, "worker-a")
    token = disks.acquire(disk_id, container_id=holder, worker_id="worker-a").lease_token
    disks.publish(_publication(disk_id, holder, token, 1, 0))
    disks.publish(_publication(disk_id, holder, token, 2, 1))

    def collect(generation: int, *, lease_token: str = token, removed: int = 25) -> None:
        disks.collect(
            disk_id,
            container_id=holder,
            lease_token=lease_token,
            generation=generation,
            stored_bytes_removed=removed,
        )

    with pytest.raises(ConflictError, match="builds on another layer"):
        collect(2)
    with pytest.raises(ConflictError, match="at generation 2"):
        collect(1)
    disks.publish(_publication(disk_id, holder, token, 3, 0))
    with pytest.raises(ConflictError, match="no longer holds"):
        collect(3, lease_token="f" * 64)

    collect(3)
    assert disks.get("box-data", workspace_id=workspace_id).stored_bytes == 5
    reacquired = disks.acquire(disk_id, container_id=holder, worker_id="worker-a")
    assert [link.generation for link in reacquired.chain] == [3]
    collect(3, lease_token=reacquired.lease_token, removed=100)
    assert disks.get("box-data", workspace_id=workspace_id).stored_bytes == 0
