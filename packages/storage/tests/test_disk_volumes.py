from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from compute.block_volumes import (
    BlockVolume,
    BlockVolumeMissingError,
    BlockVolumeProvider,
    BlockVolumeRequest,
    BlockVolumeScope,
    BlockVolumeState,
)
from database.repositories.disk_volumes import DiskVolumeHost, DiskVolumeRepository
from database.repositories.orchestration import ContainerRepository
from database.tables.orchestration import ContainerTable
from shared.containers import ContainerRecord, ContainerStatus
from shared.disks import DISK_VOLUME_CACHE_SECONDS, DiskMount
from shared.timestamps import utc_now
from storage.disk_volumes import DISK_VOLUME_RELEASE_GRACE_SECONDS, DiskVolumeService
from storage.disks import DiskDeletionService, get_or_create_disks
from tests.workspaces import on_team_plan

GIB = 1024**3


@dataclass
class _Volume:
    request: BlockVolumeRequest
    attached_to: str = ""


@dataclass
class _Provider(BlockVolumeProvider):
    """One account's volumes, answering the way the provider's own record would."""

    volumes: dict[str, _Volume] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    lose_create_answer: bool = False

    def create_volume(self, request: BlockVolumeRequest) -> BlockVolume:
        self.calls.append("create")
        found = [key for key, item in self.volumes.items() if item.request.token == request.token]
        volume_id = found[0] if found else f"vol-{uuid4().hex[:12]}"
        self.volumes.setdefault(volume_id, _Volume(request))
        if self.lose_create_answer:
            self.lose_create_answer = False
            raise TimeoutError("the answer to CreateVolume was lost")
        return self._view(volume_id)

    def attach_volume(self, volume_id: str, *, instance_id: str) -> None:
        self.calls.append("attach")
        volume = self.volumes.get(volume_id)
        if volume is None:
            raise BlockVolumeMissingError(volume_id)
        assert volume.attached_to in {"", instance_id}
        volume.attached_to = instance_id

    def detach_volume(self, volume_id: str, *, instance_id: str) -> None:
        self.calls.append("detach")
        volume = self.volumes.get(volume_id)
        if volume is not None:
            assert volume.attached_to in {"", instance_id}
            volume.attached_to = ""

    def delete_volume(self, volume_id: str) -> None:
        self.calls.append("delete")
        volume = self.volumes.get(volume_id)
        assert volume is None or not volume.attached_to
        self.volumes.pop(volume_id, None)

    def describe_volumes(self, *, deployment: str) -> tuple[BlockVolume, ...]:
        return tuple(self._view(volume_id) for volume_id in self.volumes)

    def _view(self, volume_id: str) -> BlockVolume:
        volume = self.volumes[volume_id]
        return BlockVolume(
            volume_id=volume_id,
            zone=volume.request.zone,
            size_bytes=volume.request.size_bytes,
            state=BlockVolumeState.InUse if volume.attached_to else BlockVolumeState.Available,
            attached_instance_id=volume.attached_to,
            owner=volume.request.owner,
        )


@dataclass
class _Providers:
    provider: _Provider

    def volumes(self, scope: BlockVolumeScope) -> BlockVolumeProvider:
        return self.provider


@dataclass
class _Clock:
    now: datetime = field(default_factory=utc_now)

    def __call__(self) -> datetime:
        return self.now


class _NoWorkerIsAbsent:
    def is_absent(self, worker_id: str) -> bool:
        return False


def _host(instance_id: str, zone: str = "use2-az1") -> DiskVolumeHost:
    return DiskVolumeHost(
        workspace_id=str(uuid4()),
        provider_ref="aws:platform",
        connection_id=None,
        region="us-east-2",
        zone=zone,
        instance_id=instance_id,
    )


def _setup(
    services: ApiServices, name: str
) -> tuple[DiskVolumeService, _Provider, _Clock, str, str]:
    with services.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
    on_team_plan(services.database, workspace_id)
    [resolved] = get_or_create_disks(
        services.database, [DiskMount(name=name, size_bytes=GIB)], workspace_id=workspace_id
    )
    provider = _Provider()
    clock = _Clock()
    volumes = DiskVolumeService(
        services.database,
        providers=_Providers(provider),
        deployment="deployment-a",
        worker_absence=_NoWorkerIsAbsent(),
        clock=clock,
    )
    return volumes, provider, clock, workspace_id, resolved.record.id


def _container(services: ApiServices, workspace_id: str) -> str:
    container_id = str(uuid4())
    with services.database.session() as session:
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name="box",
                image="image",
                command=[],
                workspace_id=workspace_id,
                runtime_worker_id="worker",
                status=ContainerStatus.Running,
            )
        )
    return container_id


def _state(services: ApiServices, disk_id: str) -> str:
    with services.database.session() as session:
        snapshot = DiskVolumeRepository(session).snapshot(disk_id)
    assert snapshot is not None
    return snapshot.state


def test_a_released_volume_is_taken_back_in_place_moved_between_zones_and_expires(
    isolated_services: ApiServices,
) -> None:
    volumes, provider, clock, workspace_id, disk_id = _setup(isolated_services, "vol-life")
    disks = isolated_services.disks
    first = _container(isolated_services, workspace_id)
    lease = disks.acquire(disk_id, container_id=first, worker_id="worker")

    grant = volumes.attach(disk_id, host=_host("i-a"), lease_token=lease.lease_token)
    again = volumes.attach(disk_id, host=_host("i-a"), lease_token=lease.lease_token)
    assert again == grant and not grant.formatted
    assert provider.calls == ["create", "attach"]
    assert provider.volumes[grant.volume_id].attached_to == "i-a"
    assert provider.volumes[grant.volume_id].request.size_bytes > GIB

    # Released and restarted on the same machine inside the grace: no detach at all.
    assert disks.release(disk_id, container_id=first, lease_token=lease.lease_token)
    volumes.release(disk_id)
    second = _container(isolated_services, workspace_id)
    lease = disks.acquire(disk_id, container_id=second, worker_id="worker")
    taken = volumes.attach(disk_id, host=_host("i-a"), lease_token=lease.lease_token)
    assert taken.volume_id == grant.volume_id and taken.formatted
    assert "detach" not in provider.calls

    # Released for good: the sweep waits out the grace, then detaches and caches it.
    assert disks.release(disk_id, container_id=second, lease_token=lease.lease_token)
    volumes.release(disk_id)
    volumes.reconcile_due(now=clock.now)
    assert _state(isolated_services, disk_id) == "releasing"
    clock.now += timedelta(seconds=DISK_VOLUME_RELEASE_GRACE_SECONDS + 1)
    volumes.reconcile_due(now=clock.now)
    assert _state(isolated_services, disk_id) == "cached"
    assert provider.volumes[grant.volume_id].attached_to == ""

    # A holder in another zone cannot use it: the stale one goes, a new one comes.
    third = _container(isolated_services, workspace_id)
    lease = disks.acquire(disk_id, container_id=third, worker_id="worker")
    moved = volumes.attach(
        disk_id, host=_host("i-b", zone="use2-az2"), lease_token=lease.lease_token
    )
    assert moved.volume_id != grant.volume_id and not moved.formatted
    assert list(provider.volumes) == [moved.volume_id]

    assert disks.release(disk_id, container_id=third, lease_token=lease.lease_token)
    volumes.release(disk_id)
    clock.now += timedelta(seconds=DISK_VOLUME_RELEASE_GRACE_SECONDS + 1)
    volumes.reconcile_due(now=clock.now)
    clock.now += timedelta(seconds=DISK_VOLUME_CACHE_SECONDS - 1)
    volumes.reconcile_due(now=clock.now)
    assert list(provider.volumes) == [moved.volume_id]
    clock.now += timedelta(seconds=2)
    volumes.reconcile_due(now=clock.now)
    assert provider.volumes == {}
    assert _state(isolated_services, disk_id) == "none"


def test_a_lost_create_answer_and_a_dead_holder_recover_without_a_second_volume(
    isolated_services: ApiServices,
) -> None:
    volumes, provider, clock, workspace_id, disk_id = _setup(isolated_services, "vol-crash")
    disks = isolated_services.disks
    holder = _container(isolated_services, workspace_id)
    lease = disks.acquire(disk_id, container_id=holder, worker_id="worker")

    provider.lose_create_answer = True
    with pytest.raises(TimeoutError):
        volumes.attach(disk_id, host=_host("i-a"), lease_token=lease.lease_token)
    assert _state(isolated_services, disk_id) == "creating"
    grant = volumes.attach(disk_id, host=_host("i-a"), lease_token=lease.lease_token)
    assert list(provider.volumes) == [grant.volume_id]

    with isolated_services.database.session() as session:
        row = session.get(ContainerTable, holder)
        assert row is not None
        row.status = ContainerStatus.Stopped.value
        row.storage_released_at = utc_now()

    # Its holder stopped and released storage without releasing the lease: the
    # sweep detaches the volume and keeps it for the next holder.
    volumes.reconcile_due(now=clock.now)
    assert _state(isolated_services, disk_id) == "cached"
    assert provider.volumes[grant.volume_id].attached_to == ""

    deletion = DiskDeletionService(
        isolated_services.database,
        disks=disks,
        volumes=volumes,
        objects=isolated_services.disk_deletion.objects,
        metering=isolated_services.disk_deletion.metering,
    )
    assert deletion.request("vol-crash", workspace_id=workspace_id)
    assert provider.volumes == {}
