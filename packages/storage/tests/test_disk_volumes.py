from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from compute.block_volumes import (
    BlockSnapshot,
    BlockSnapshotMissingError,
    BlockSnapshotRequest,
    BlockSnapshotState,
    BlockVolume,
    BlockVolumeMissingError,
    BlockVolumeOwner,
    BlockVolumePendingError,
    BlockVolumeProvider,
    BlockVolumeRequest,
    BlockVolumeScope,
    BlockVolumeState,
)
from database.repositories.disk_volumes import DiskVolumeHost, DiskVolumeRepository
from database.repositories.orchestration import ContainerRepository
from database.tables.orchestration import ContainerTable
from shared.containers import ContainerRecord, ContainerStatus
from shared.disks import DISK_VOLUME_CACHE_SECONDS, DiskMount, disk_manifest_key
from shared.errors import ConflictError, DiskVolumePendingError
from shared.timestamps import utc_now
from storage.disk_snapshots import DISK_SNAPSHOT_POLL_SECONDS, DiskSnapshotService
from storage.disk_volumes import (
    DISK_VOLUME_LEASE_SILENCE_SECONDS,
    DISK_VOLUME_ORPHAN_MIN_AGE_SECONDS,
    DISK_VOLUME_RECHECK_SECONDS,
    DISK_VOLUME_RELEASE_GRACE_SECONDS,
    DiskVolumeService,
)
from storage.disks import DiskDeletionService, DiskPublication, DiskService, get_or_create_disks
from tests.workspaces import on_team_plan

GIB = 1024**3


@dataclass
class _Volume:
    request: BlockVolumeRequest
    attached_to: str = ""
    created_at: datetime = field(default_factory=utc_now)


@dataclass
class _Snapshot:
    request: BlockSnapshotRequest
    volume_size_bytes: int
    state: BlockSnapshotState = BlockSnapshotState.Pending
    created_at: datetime = field(default_factory=utc_now)


SNAPSHOT_STORED_BYTES = 3 * GIB


@dataclass
class _Provider(BlockVolumeProvider):
    """One account's volumes, answering the way the provider's own record would."""

    volumes: dict[str, _Volume] = field(default_factory=dict)
    snapshots: dict[str, _Snapshot] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    lose_create_answer: bool = False
    lose_snapshot_answer: bool = False
    attach_pending: bool = False
    while_detaching: Callable[[], None] | None = None

    def create_volume(self, request: BlockVolumeRequest, *, wait_seconds: float) -> BlockVolume:
        self.calls.append("create")
        if request.snapshot_id and request.snapshot_id not in self.snapshots:
            raise BlockSnapshotMissingError(request.snapshot_id)
        found = [key for key, item in self.volumes.items() if item.request.token == request.token]
        volume_id = found[0] if found else f"vol-{uuid4().hex[:12]}"
        self.volumes.setdefault(volume_id, _Volume(request))
        if self.lose_create_answer:
            self.lose_create_answer = False
            raise TimeoutError("the answer to CreateVolume was lost")
        return self._view(volume_id)

    def attach_volume(self, volume_id: str, *, instance_id: str, wait_seconds: float) -> None:
        self.calls.append("attach")
        volume = self.volumes.get(volume_id)
        if volume is None:
            raise BlockVolumeMissingError(volume_id)
        assert volume.attached_to in {"", instance_id}
        volume.attached_to = instance_id
        if self.attach_pending:
            self.attach_pending = False
            raise BlockVolumePendingError(volume_id)

    def detach_volume(self, volume_id: str, *, instance_id: str, wait_seconds: float) -> None:
        self.calls.append("detach")
        if self.while_detaching is not None:
            hook, self.while_detaching = self.while_detaching, None
            hook()
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

    def create_snapshot(self, request: BlockSnapshotRequest) -> BlockSnapshot:
        self.calls.append("snapshot")
        found = self.find_snapshot(token=request.token)
        if found is not None:
            return found
        snapshot_id = f"snap-{uuid4().hex[:12]}"
        size = self.volumes[request.volume_id].request.size_bytes
        self.snapshots[snapshot_id] = _Snapshot(request, volume_size_bytes=size)
        if self.lose_snapshot_answer:
            self.lose_snapshot_answer = False
            raise TimeoutError("the answer to CreateSnapshot was lost")
        return self._snapshot_view(snapshot_id)

    def find_snapshot(self, *, token: str) -> BlockSnapshot | None:
        for snapshot_id, snapshot in self.snapshots.items():
            if snapshot.request.token == token:
                return self._snapshot_view(snapshot_id)
        return None

    def describe_snapshot(self, snapshot_id: str) -> BlockSnapshot | None:
        return self._snapshot_view(snapshot_id) if snapshot_id in self.snapshots else None

    def delete_snapshot(self, snapshot_id: str) -> None:
        self.calls.append("delete snapshot")
        self.snapshots.pop(snapshot_id, None)

    def describe_snapshots(self, *, deployment: str) -> tuple[BlockSnapshot, ...]:
        return tuple(self._snapshot_view(snapshot_id) for snapshot_id in self.snapshots)

    def complete_snapshots(self) -> None:
        for snapshot in self.snapshots.values():
            snapshot.state = BlockSnapshotState.Completed

    def _snapshot_view(self, snapshot_id: str) -> BlockSnapshot:
        snapshot = self.snapshots[snapshot_id]
        return BlockSnapshot(
            snapshot_id=snapshot_id,
            state=snapshot.state,
            volume_size_bytes=snapshot.volume_size_bytes,
            stored_bytes=SNAPSHOT_STORED_BYTES,
            owner=snapshot.request.owner,
            creation_token=snapshot.request.token,
            created_at=snapshot.created_at,
        )

    def _view(self, volume_id: str) -> BlockVolume:
        volume = self.volumes[volume_id]
        return BlockVolume(
            volume_id=volume_id,
            zone=volume.request.zone,
            size_bytes=volume.request.size_bytes,
            state=BlockVolumeState.InUse if volume.attached_to else BlockVolumeState.Available,
            attached_instance_id=volume.attached_to,
            owner=volume.request.owner,
            creation_token=volume.request.token,
            created_at=volume.created_at,
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


@dataclass
class _WorkerAbsence:
    absent: bool = False
    when_asked: Callable[[], None] | None = None

    def is_absent(self, worker_id: str) -> bool:
        if self.when_asked is not None:
            self.when_asked()
        return self.absent


def _host(
    instance_id: str,
    zone: str = "use2-az1",
    connection_id: str | None = None,
    region: str = "us-east-2",
) -> DiskVolumeHost:
    return DiskVolumeHost(
        workspace_id=str(uuid4()),
        provider_ref="aws:platform",
        connection_id=connection_id,
        region=region,
        zone=zone,
        instance_id=instance_id,
    )


def _setup(
    services: ApiServices, name: str, absence: _WorkerAbsence | None = None
) -> tuple[DiskVolumeService, _Provider, _Clock, str, str]:
    with services.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
    on_team_plan(services.database, workspace_id)
    [resolved] = get_or_create_disks(
        services.database,
        [DiskMount(name=name, size_bytes=GIB)],
        workspace_id=workspace_id,
        stub_id=str(uuid4()),
    )
    provider = _Provider()
    clock = _Clock()
    volumes = DiskVolumeService(
        services.database,
        providers=_Providers(provider),
        deployment="deployment-a",
        worker_absence=absence or _WorkerAbsence(),
        clock=clock,
    )
    return volumes, provider, clock, workspace_id, resolved.record.id


def _snapshots(services: ApiServices, volumes: DiskVolumeService) -> DiskSnapshotService:
    return DiskSnapshotService(
        services.database,
        providers=volumes.providers,
        deployment=volumes.deployment,
        clock=volumes.clock,
    )


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

    # Released and restarted on the same machine inside the grace, so nothing detaches.
    assert disks.release(disk_id, container_id=first, lease_token=lease.lease_token)
    volumes.release(disk_id)
    second = _container(isolated_services, workspace_id)
    lease = disks.acquire(disk_id, container_id=second, worker_id="worker")
    taken = volumes.attach(disk_id, host=_host("i-a"), lease_token=lease.lease_token)
    assert taken.volume_id == grant.volume_id and taken.formatted
    assert "detach" not in provider.calls

    # Released for good. The sweep waits out the grace, then detaches and caches it.
    assert disks.release(disk_id, container_id=second, lease_token=lease.lease_token)
    volumes.release(disk_id)
    volumes.reconcile_due(now=clock.now)
    assert _state(isolated_services, disk_id) == "releasing"
    clock.now += timedelta(seconds=DISK_VOLUME_RELEASE_GRACE_SECONDS + 1)
    volumes.reconcile_due(now=clock.now)
    assert _state(isolated_services, disk_id) == "cached"
    assert provider.volumes[grant.volume_id].attached_to == ""

    # A holder in another zone cannot use it, so the stale volume goes and a new one comes.
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

    # The retried acquire keeps its lease, and an attach still under way answers
    # pending rather than holding the request past the worker's timeout.
    retried = disks.acquire(disk_id, container_id=holder, worker_id="worker")
    assert retried.lease_token == lease.lease_token
    provider.attach_pending = True
    with pytest.raises(DiskVolumePendingError):
        volumes.attach(disk_id, host=_host("i-a"), lease_token=lease.lease_token)
    grant = volumes.attach(disk_id, host=_host("i-a"), lease_token=lease.lease_token)
    assert list(provider.volumes) == [grant.volume_id]
    assert provider.calls.count("create") == 2

    with isolated_services.database.session() as session:
        row = session.get(ContainerTable, holder)
        assert row is not None
        row.status = ContainerStatus.Stopped.value
        row.storage_released_at = utc_now()

    # Its holder stopped and released storage without releasing the lease. The
    # sweep detaches the volume and keeps it for the next holder.
    volumes.reconcile_due(now=clock.now)
    assert _state(isolated_services, disk_id) == "cached"
    assert provider.volumes[grant.volume_id].attached_to == ""

    deletion = DiskDeletionService(
        isolated_services.database,
        disks=disks,
        volumes=volumes,
        snapshots=_snapshots(isolated_services, volumes),
        objects=isolated_services.disk_deletion.objects,
        metering=isolated_services.disk_deletion.metering,
    )
    disks.request_deletion("vol-crash", workspace_id=workspace_id, now=clock.now)
    assert list(provider.volumes) == [grant.volume_id]
    deletion.reconcile_due(now=clock.now)
    assert provider.volumes == {}
    assert not disks.list(workspace_id=workspace_id).data


@dataclass
class _Objects:
    failing: str
    attempts: list[str] = field(default_factory=list)

    def delete_disk_objects(self, *, workspace_id: str, disk_id: str) -> None:
        self.attempts.append(disk_id)
        if disk_id == self.failing:
            raise RuntimeError("the bucket refused the delete")


def test_a_deletion_that_keeps_failing_backs_off_without_holding_up_newer_ones(
    isolated_services: ApiServices,
) -> None:
    volumes, _, clock, workspace_id, failing = _setup(isolated_services, "del-failing")
    disks = isolated_services.disks
    [newer] = get_or_create_disks(
        isolated_services.database,
        [DiskMount(name="del-newer", size_bytes=GIB)],
        workspace_id=workspace_id,
        stub_id=str(uuid4()),
    )
    objects = _Objects(failing=failing)
    deletion = DiskDeletionService(
        isolated_services.database,
        disks=disks,
        volumes=volumes,
        snapshots=_snapshots(isolated_services, volumes),
        objects=objects,
        metering=isolated_services.disk_deletion.metering,
    )
    requested = clock.now
    disks.request_deletion("del-failing", workspace_id=workspace_id, now=requested)
    disks.request_deletion(
        "del-newer", workspace_id=workspace_id, now=requested + timedelta(seconds=1)
    )

    def attempts_at(seconds: int) -> list[str]:
        deletion.reconcile_due(now=requested + timedelta(seconds=seconds), limit=1)
        return objects.attempts

    assert attempts_at(1) == [failing]
    assert attempts_at(1) == [failing, newer.record.id]
    # Retried after the shortest wait, then after a wait as long as its age.
    assert attempts_at(30) == [failing, newer.record.id]
    assert attempts_at(31) == [failing, newer.record.id, failing]
    assert attempts_at(61) == [failing, newer.record.id, failing]
    assert attempts_at(62) == [failing, newer.record.id, failing, failing]


def test_a_sweep_that_loses_the_disk_to_a_new_holder_leaves_its_volume_attached(
    isolated_services: ApiServices,
) -> None:
    absence = _WorkerAbsence(absent=True)
    volumes, provider, clock, workspace_id, disk_id = _setup(isolated_services, "vol-race", absence)
    disks = isolated_services.disks
    first = _container(isolated_services, workspace_id)
    lease = disks.acquire(disk_id, container_id=first, worker_id="worker")
    grant = volumes.attach(disk_id, host=_host("i-a"), lease_token=lease.lease_token)
    with isolated_services.database.session() as session:
        row = session.get(ContainerTable, first)
        assert row is not None
        row.status = ContainerStatus.Stopped.value

    # The sweep finds the holder's worker gone and decides to detach; while it
    # does, a new container takes the disk on the same machine.
    taken_over: list[str] = []

    def take_over() -> None:
        absence.when_asked = None
        second = _container(isolated_services, workspace_id)
        taker = DiskService(isolated_services.database, worker_absence=absence)
        taken = taker.acquire(disk_id, container_id=second, worker_id="worker")
        taken_over.append(
            volumes.attach(disk_id, host=_host("i-a"), lease_token=taken.lease_token).volume_id
        )

    absence.when_asked = take_over
    clock.now += timedelta(seconds=DISK_VOLUME_RECHECK_SECONDS + 1)
    volumes.reconcile_due(now=clock.now)

    assert taken_over == [grant.volume_id]
    assert "detach" not in provider.calls
    assert provider.volumes[grant.volume_id].attached_to == "i-a"
    assert _state(isolated_services, disk_id) == "attached"


def _stop(services: ApiServices, container_id: str, *, storage_released: bool) -> None:
    with services.database.session() as session:
        row = session.get(ContainerTable, container_id)
        assert row is not None
        row.status = ContainerStatus.Stopped.value
        if storage_released:
            row.storage_released_at = utc_now()


def test_a_new_holder_waits_out_a_sweeps_detach_and_then_takes_the_volume_back(
    isolated_services: ApiServices,
) -> None:
    volumes, provider, clock, workspace_id, disk_id = _setup(isolated_services, "vol-handoff")
    disks = isolated_services.disks
    first = _container(isolated_services, workspace_id)
    lease = disks.acquire(disk_id, container_id=first, worker_id="worker")
    grant = volumes.attach(disk_id, host=_host("i-a"), lease_token=lease.lease_token)
    _stop(isolated_services, first, storage_released=True)

    # The sweep's detach is under way when a new container takes the disk.
    second = _container(isolated_services, workspace_id)
    waited: list[str] = []

    def take_the_disk() -> None:
        lease = disks.acquire(disk_id, container_id=second, worker_id="worker")
        with pytest.raises(DiskVolumePendingError):
            volumes.attach(disk_id, host=_host("i-a"), lease_token=lease.lease_token)
        waited.append(lease.lease_token)

    provider.while_detaching = take_the_disk
    volumes.reconcile_due(now=clock.now)
    assert len(waited) == 1

    # The detach it made is recorded, so the holder continues at once.
    assert _state(isolated_services, disk_id) == "cached"
    back = volumes.attach(disk_id, host=_host("i-a"), lease_token=waited[0])
    assert back.volume_id == grant.volume_id and back.formatted
    assert provider.volumes[grant.volume_id].attached_to == "i-a"


def test_housekeeping_that_leaves_a_volume_alone_does_not_keep_its_dead_driver_alive(
    isolated_services: ApiServices,
) -> None:
    volumes, provider, clock, workspace_id, disk_id = _setup(isolated_services, "vol-dead")
    disks = isolated_services.disks
    first = _container(isolated_services, workspace_id)
    lease = disks.acquire(disk_id, container_id=first, worker_id="worker")
    provider.attach_pending = True
    with pytest.raises(DiskVolumePendingError):
        volumes.attach(disk_id, host=_host("i-a"), lease_token=lease.lease_token)
    _stop(isolated_services, first, storage_released=True)

    # A new holder on the same machine finds the first lease's attach unfinished.
    second = _container(isolated_services, workspace_id)
    taken = disks.acquire(disk_id, container_id=second, worker_id="worker")
    with pytest.raises(DiskVolumePendingError):
        volumes.attach(disk_id, host=_host("i-a"), lease_token=taken.lease_token)

    # The dead lease's attach is left alone for as long as its own call could run.
    clock.now += timedelta(seconds=DISK_VOLUME_LEASE_SILENCE_SECONDS - 1)
    with pytest.raises(DiskVolumePendingError):
        volumes.attach(disk_id, host=_host("i-a"), lease_token=taken.lease_token)

    # Past that, housekeeping sees a live holder and leaves the volume; the
    # holder's next ask takes the attach over, well inside its acquire deadline.
    clock.now += timedelta(seconds=2)
    volumes.reconcile_due(now=clock.now)
    assert _state(isolated_services, disk_id) == "attaching"
    grant = volumes.attach(disk_id, host=_host("i-a"), lease_token=taken.lease_token)
    assert provider.volumes[grant.volume_id].attached_to == "i-a"
    assert _state(isolated_services, disk_id) == "attached"


def test_an_abandoned_creation_outlives_its_disk_until_its_volume_is_collected(
    isolated_services: ApiServices,
) -> None:
    volumes, provider, clock, workspace_id, disk_id = _setup(isolated_services, "vol-orphan")
    disks = isolated_services.disks
    connection_id = str(uuid4())
    holder = _container(isolated_services, workspace_id)
    lease = disks.acquire(disk_id, container_id=holder, worker_id="worker")
    provider.lose_create_answer = True
    with pytest.raises(TimeoutError):
        volumes.attach(
            disk_id, host=_host("i-a", connection_id=connection_id), lease_token=lease.lease_token
        )
    [made] = provider.volumes
    _stop(isolated_services, holder, storage_released=True)

    deletion = DiskDeletionService(
        isolated_services.database,
        disks=disks,
        volumes=volumes,
        snapshots=_snapshots(isolated_services, volumes),
        objects=isolated_services.disk_deletion.objects,
        metering=isolated_services.disk_deletion.metering,
    )
    disks.request_deletion("vol-orphan", workspace_id=workspace_id, now=clock.now)
    clock.now += timedelta(seconds=DISK_VOLUME_LEASE_SILENCE_SECONDS + 1)
    deletion.reconcile_due(now=clock.now)
    assert not disks.list(workspace_id=workspace_id).data

    # The disk is gone, and so is the only row naming this account; the
    # creation it abandoned still holds the connection and is still collected.
    with isolated_services.database.session() as session:
        assert DiskVolumeRepository(session).connection_holds_volumes(connection_id)
    clock.now += timedelta(seconds=DISK_VOLUME_ORPHAN_MIN_AGE_SECONDS)
    assert volumes.collect_orphans() == 1
    assert made not in provider.volumes
    with isolated_services.database.session() as session:
        assert not DiskVolumeRepository(session).connection_holds_volumes(connection_id)


def _publish(
    services: ApiServices, disk_id: str, container_id: str, token: str, generation: int
) -> None:
    services.disks.publish(
        DiskPublication(
            disk_id=disk_id,
            container_id=container_id,
            lease_token=token,
            generation=generation,
            parent_generation=generation - 1,
            manifest_key=disk_manifest_key(disk_id, generation),
            manifest_sha256=f"{generation:x}".rjust(64, "0"),
            stored_bytes_added=10,
        )
    )


def test_only_the_holder_snapshots_its_newest_generation_and_a_retry_finds_the_first(
    isolated_services: ApiServices,
) -> None:
    volumes, provider, _, workspace_id, disk_id = _setup(isolated_services, "snap-fence")
    snapshots = _snapshots(isolated_services, volumes)
    holder = _container(isolated_services, workspace_id)
    lease = isolated_services.disks.acquire(disk_id, container_id=holder, worker_id="worker")
    volumes.attach(disk_id, host=_host("i-a"), lease_token=lease.lease_token)
    _publish(isolated_services, disk_id, holder, lease.lease_token, 1)

    def take(generation: int, *, final: bool = False, token: str = lease.lease_token) -> str:
        taken = snapshots.take(
            disk_id, container_id=holder, lease_token=token, generation=generation, final=final
        )
        return taken.snapshot_id if taken.taken else ""

    # CreateSnapshot has no client token; the row's token in the tags is how a
    # retry after a lost answer finds the snapshot instead of making a second.
    provider.lose_snapshot_answer = True
    with pytest.raises(TimeoutError):
        take(1)
    first = take(1)
    assert first and take(1) == first and list(provider.snapshots) == [first]

    with pytest.raises(ConflictError):
        take(1, token="a-lease-that-was-taken-over")
    with pytest.raises(ConflictError):
        take(2)

    # A periodic snapshot waits for the pending one; the release's does not.
    _publish(isolated_services, disk_id, holder, lease.lease_token, 2)
    assert take(2) == ""
    final = take(2, final=True)
    assert final and final != first and len(provider.snapshots) == 2


def test_a_new_volume_starts_from_the_newest_completed_snapshot_in_its_region(
    isolated_services: ApiServices,
) -> None:
    volumes, provider, clock, workspace_id, disk_id = _setup(isolated_services, "snap-restore")
    disks = isolated_services.disks
    snapshots = _snapshots(isolated_services, volumes)
    holder = _container(isolated_services, workspace_id)
    lease = disks.acquire(disk_id, container_id=holder, worker_id="worker")
    volumes.attach(disk_id, host=_host("i-a"), lease_token=lease.lease_token)

    def snapshot_generation(generation: int) -> str:
        _publish(isolated_services, disk_id, holder, lease.lease_token, generation)
        taken = snapshots.take(
            disk_id,
            container_id=holder,
            lease_token=lease.lease_token,
            generation=generation,
            final=False,
        )
        provider.complete_snapshots()
        clock.now += timedelta(seconds=DISK_SNAPSHOT_POLL_SECONDS)
        snapshots.reconcile_due(now=clock.now)
        return taken.snapshot_id

    older = snapshot_generation(1)
    newest = snapshot_generation(2)
    # Completing the newest marked the older for deletion; the next pass deletes it.
    snapshots.reconcile_due(now=clock.now)
    assert list(provider.snapshots) == [newest] and older != newest
    # Both generations' chunks, and the snapshot beside them.
    assert disks.get("snap-restore", workspace_id=workspace_id).stored_bytes == (
        20 + SNAPSHOT_STORED_BYTES
    )

    def start_elsewhere(host: DiskVolumeHost) -> str:
        nonlocal holder, lease
        assert disks.release(disk_id, container_id=holder, lease_token=lease.lease_token)
        volumes.release(disk_id)
        holder = _container(isolated_services, workspace_id)
        lease = disks.acquire(disk_id, container_id=holder, worker_id="worker")
        grant = volumes.attach(disk_id, host=host, lease_token=lease.lease_token)
        return provider.volumes[grant.volume_id].request.snapshot_id

    # The cached volume is in another zone, so a new one is made, from the snapshot.
    assert start_elsewhere(_host("i-b", zone="use2-az2")) == newest
    # A snapshot stays in its region; another region restores from object storage.
    assert start_elsewhere(_host("i-c", zone="usw1-az1", region="us-west-1")) == ""
    # A snapshot deleted behind the platform's back makes a blank volume, not a failure.
    provider.snapshots.clear()
    assert start_elsewhere(_host("i-d", zone="use2-az3")) == ""


def test_deleting_a_disk_deletes_its_snapshots_and_collection_deletes_strays(
    isolated_services: ApiServices,
) -> None:
    volumes, provider, clock, workspace_id, disk_id = _setup(isolated_services, "snap-delete")
    disks = isolated_services.disks
    snapshots = _snapshots(isolated_services, volumes)
    holder = _container(isolated_services, workspace_id)
    lease = disks.acquire(disk_id, container_id=holder, worker_id="worker")
    grant = volumes.attach(disk_id, host=_host("i-a"), lease_token=lease.lease_token)
    _publish(isolated_services, disk_id, holder, lease.lease_token, 1)
    taken = snapshots.take(
        disk_id, container_id=holder, lease_token=lease.lease_token, generation=1, final=True
    )
    provider.snapshots["snap-stray"] = _Snapshot(
        BlockSnapshotRequest(
            owner=BlockVolumeOwner(
                deployment="deployment-a", workspace_id=workspace_id, disk_id=str(uuid4())
            ),
            volume_id=grant.volume_id,
            generation=1,
            token="a-creation-no-row-records",
        ),
        volume_size_bytes=GIB,
        state=BlockSnapshotState.Completed,
        created_at=clock.now - timedelta(hours=1),
    )
    assert snapshots.collect_orphans() == 1
    assert list(provider.snapshots) == [taken.snapshot_id]

    _stop(isolated_services, holder, storage_released=True)
    deletion = DiskDeletionService(
        isolated_services.database,
        disks=disks,
        volumes=volumes,
        snapshots=snapshots,
        objects=isolated_services.disk_deletion.objects,
        metering=isolated_services.disk_deletion.metering,
    )
    disks.request_deletion("snap-delete", workspace_id=workspace_id, now=clock.now)
    deletion.reconcile_due(now=clock.now)
    assert provider.snapshots == {} and provider.volumes == {}
    assert not disks.list(workspace_id=workspace_id).data
