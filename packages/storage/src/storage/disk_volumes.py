"""Each disk's provider volume, from creation through attach, cache and deletion.

While a container holds a disk on a provider machine, the disk lives on its own
block volume attached to that machine. Released, the volume stays detached for
`DISK_VOLUME_CACHE_SECONDS` so a restart in the same zone attaches it again
instead of restoring from object storage; after that it is deleted. The object
store holds the durable copy throughout, so deleting a volume never loses data.
A joined machine has no provider volume and keeps its disks in host storage.

The disk row records the volume's state, and every provider call happens outside
any transaction. A step reads the row, calls the provider, then records the
result only if the row is still exactly as it read it: the revision fences
concurrent drivers and the lease token fences a container that has lost the
disk. Every provider call is idempotent against the provider's own record, so a
driver that crashes between the call and the record is finished by the next one,
whether that is the holder retrying or the housekeeping sweep.

`releasing` is the lease let go with the volume still attached. The worker
unmounts it after its final release, so the detach waits out a grace rather than
pulling the device from under it; a restart on the same machine inside that
grace takes the volume back without a detach at all.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from compute.block_volumes import (
    BlockVolumeMissingError,
    BlockVolumeOwner,
    BlockVolumeProvider,
    BlockVolumeProviders,
    BlockVolumeRequest,
    BlockVolumeScope,
    orphaned_volumes,
)
from database.repositories.disk_volumes import (
    DiskVolumeHost,
    DiskVolumeRepository,
    DiskVolumeSnapshot,
)
from database.repositories.disks import DiskHolder, DiskRepository
from shared.containers import LIVE_CONTAINER_STATUSES
from shared.disks import (
    DISK_VOLUME_CACHE_SECONDS,
    DISK_VOLUME_THROUGHPUT_MIBPS,
    disk_volume_size_bytes,
)
from shared.errors import ConflictError, NotFoundError, UpstreamUnavailableError
from shared.timestamps import to_utc, utc_now

from database import DatabaseClient

LOGGER = logging.getLogger(__name__)

DISK_VOLUME_RELEASE_GRACE_SECONDS = 30
"""How long a released volume stays attached for its worker to unmount it."""

DISK_VOLUME_STUCK_SECONDS = 10 * 60
"""Age at which a volume transition has plainly lost whoever was driving it.

Longer than any bounded provider wait, so the sweep never races a live driver.
"""

DISK_VOLUME_ORPHAN_INTERVAL_SECONDS = 60 * 60
DISK_VOLUME_ORPHAN_MIN_AGE_SECONDS = DISK_VOLUME_STUCK_SECONDS
_MAX_STEPS = 16


class VolumeState(StrEnum):
    None_ = "none"
    Creating = "creating"
    Attaching = "attaching"
    Attached = "attached"
    Releasing = "releasing"
    Detaching = "detaching"
    Cached = "cached"
    Deleting = "deleting"


class DiskWorkerAbsence(Protocol):
    def is_absent(self, worker_id: str) -> bool: ...


def holder_keeps_disk(holder: DiskHolder, absence: DiskWorkerAbsence) -> bool:
    """Whether the container named on a disk can still publish to it.

    A stopped container keeps the disk until its worker has released its storage
    or is gone for good, because its final publish may still be on the way.
    """
    if holder.status is None:
        return False
    if holder.status in LIVE_CONTAINER_STATUSES:
        return True
    if holder.storage_released or not holder.worker_id:
        return False
    return not absence.is_absent(holder.worker_id)


@dataclass(frozen=True, slots=True)
class DiskVolumeGrant:
    volume_id: str
    formatted: bool


@dataclass(frozen=True, slots=True)
class AttachTo:
    """Have the volume attached to this machine, at least this large, under this lease."""

    host: DiskVolumeHost
    size_bytes: int
    lease_token: str


@dataclass(frozen=True, slots=True)
class Detach:
    """Have the volume attached nowhere; a detached one stays cached."""


@dataclass(frozen=True, slots=True)
class Remove:
    """Have no volume at all."""


type VolumeGoal = AttachTo | Detach | Remove


class StepKind(StrEnum):
    BeginCreate = "begin_create"
    Create = "create"
    Forget = "forget"
    Attach = "attach"
    Reattach = "reattach"
    BeginDetach = "begin_detach"
    Detach = "detach"
    BeginDelete = "begin_delete"
    Delete = "delete"


def next_step(snapshot: DiskVolumeSnapshot, goal: VolumeGoal) -> StepKind | None:
    """The one step that moves the volume toward the goal, or None once it is there."""
    state = VolumeState(snapshot.state)
    if isinstance(goal, AttachTo):
        fits = (
            snapshot.provider_ref == goal.host.provider_ref
            and snapshot.region == goal.host.region
            and snapshot.zone == goal.host.zone
            and snapshot.volume_size_bytes >= goal.size_bytes
        )
        here = fits and snapshot.instance_id == goal.host.instance_id
        match state:
            case VolumeState.None_:
                return StepKind.BeginCreate
            case VolumeState.Creating:
                # A creation for another machine belongs to an attempt that ended.
                return StepKind.Create if here else StepKind.Forget
            case VolumeState.Attaching:
                return StepKind.Attach if here else StepKind.BeginDetach
            case VolumeState.Attached:
                return None if here else StepKind.BeginDetach
            case VolumeState.Releasing:
                return StepKind.Reattach if here else StepKind.BeginDetach
            case VolumeState.Detaching:
                return StepKind.Detach
            case VolumeState.Cached:
                return StepKind.Reattach if fits else StepKind.BeginDelete
            case VolumeState.Deleting:
                return StepKind.Delete
    match state:
        case VolumeState.None_:
            return None
        case VolumeState.Creating:
            return StepKind.Forget
        case VolumeState.Attaching | VolumeState.Attached | VolumeState.Releasing:
            return StepKind.BeginDetach
        case VolumeState.Detaching:
            return StepKind.Detach
        case VolumeState.Cached:
            return StepKind.BeginDelete if isinstance(goal, Remove) else None
        case VolumeState.Deleting:
            return StepKind.Delete


@dataclass(slots=True)
class DiskVolumeService:
    database: DatabaseClient
    providers: BlockVolumeProviders
    deployment: str
    """The platform deployment's namespace, stamped on every volume it creates."""

    worker_absence: DiskWorkerAbsence
    clock: Callable[[], datetime] = utc_now
    _orphans_due_at: datetime | None = field(default=None, init=False)

    def host(self, worker_id: str) -> DiskVolumeHost | None:
        with self.database.session() as session:
            return DiskVolumeRepository(session).host_for_worker(worker_id)

    def attach(self, disk_id: str, *, host: DiskVolumeHost, lease_token: str) -> DiskVolumeGrant:
        """Attach the disk's volume to the holder's machine, making or moving it as needed."""
        snapshot = self._snapshot(disk_id)
        goal = AttachTo(
            host=host,
            size_bytes=disk_volume_size_bytes(snapshot.size_bytes),
            lease_token=lease_token,
        )
        settled = self._drive(disk_id, goal)
        return DiskVolumeGrant(volume_id=settled.volume_id, formatted=settled.formatted)

    def release(self, disk_id: str) -> None:
        """Mark a released disk's volume for detaching once its worker has unmounted it.

        Only a volume whose lease is already gone moves; a disk taken again by
        another container meanwhile keeps its volume for that container's attach.
        """
        with self.database.session() as session:
            repository = DiskVolumeRepository(session)
            snapshot = repository.snapshot(disk_id)
            if snapshot is None or snapshot.lease_token:
                return
            if snapshot.state not in {VolumeState.Attaching, VolumeState.Attached}:
                return
            repository.transition(snapshot, at=self.clock(), state=VolumeState.Releasing)

    def remove(self, disk_id: str) -> None:
        """Detach and delete the disk's volume; raises until the provider confirms both."""
        if self._snapshot_or_none(disk_id) is not None:
            self._drive(disk_id, Remove())

    def reconcile_due(self, *, now: datetime | None = None, limit: int = 100) -> None:
        current = to_utc(now or self.clock())
        with self.database.session() as session:
            due = DiskVolumeRepository(session).due(
                cached_before=current - timedelta(seconds=DISK_VOLUME_CACHE_SECONDS),
                released_before=current - timedelta(seconds=DISK_VOLUME_RELEASE_GRACE_SECONDS),
                stuck_before=current - timedelta(seconds=DISK_VOLUME_STUCK_SECONDS),
                limit=limit,
            )
        for disk_id in due:
            try:
                self._settle(disk_id, now=current)
            except Exception:
                LOGGER.exception(
                    "disk volume housekeeping failed; retrying next pass",
                    extra={"disk_id": disk_id},
                )
        self._collect_orphans_when_due(now=current)

    def collect_orphans(self) -> int:
        """Delete this deployment's volumes that no disk records; returns how many."""
        with self.database.session() as session:
            scopes = DiskVolumeRepository(session).scopes()
        removed = 0
        for scope in scopes:
            provider = self.providers.volumes(
                BlockVolumeScope(
                    workspace_id=scope.workspace_id,
                    provider_ref=scope.provider_ref,
                    region=scope.region,
                )
            )
            volumes = provider.describe_volumes(deployment=self.deployment)
            cutoff = self.clock() - timedelta(seconds=DISK_VOLUME_ORPHAN_MIN_AGE_SECONDS)
            settled = tuple(
                volume
                for volume in volumes
                if volume.created_at is not None and to_utc(volume.created_at) <= cutoff
            )
            disk_ids = sorted({volume.owner.disk_id for volume in settled if volume.owner})
            with self.database.session() as session:
                recorded, creating = DiskVolumeRepository(session).recorded(disk_ids)
            for orphan in orphaned_volumes(
                settled, deployment=self.deployment, recorded=recorded, creating=creating
            ):
                LOGGER.warning(
                    "deleting a disk volume no disk records",
                    extra={
                        "volume_id": orphan.volume_id,
                        "disk_id": orphan.owner.disk_id if orphan.owner else "",
                        "provider_ref": scope.provider_ref,
                        "region": scope.region,
                    },
                )
                provider.delete_volume(orphan.volume_id)
                removed += 1
        return removed

    def _collect_orphans_when_due(self, *, now: datetime) -> None:
        if self._orphans_due_at is not None and now < self._orphans_due_at:
            return
        self._orphans_due_at = now + timedelta(seconds=DISK_VOLUME_ORPHAN_INTERVAL_SECONDS)
        try:
            self.collect_orphans()
        except Exception:
            LOGGER.exception("disk volume orphan collection failed; retrying next interval")

    def _settle(self, disk_id: str, *, now: datetime) -> None:
        snapshot = self._snapshot_or_none(disk_id)
        if snapshot is None:
            return
        state = VolumeState(snapshot.state)
        if snapshot.deleted:
            self._drive(disk_id, Remove())
            return
        if state is VolumeState.Cached:
            if snapshot.changed_at is not None and now - snapshot.changed_at >= timedelta(
                seconds=DISK_VOLUME_CACHE_SECONDS
            ):
                self._drive(disk_id, Remove())
            return
        if snapshot.lease_token and state in {
            VolumeState.Attaching,
            VolumeState.Attached,
            VolumeState.Releasing,
        }:
            with self.database.session() as session:
                holder = DiskRepository(session).holder(snapshot.holder_container_id)
            if holder_keeps_disk(holder, self.worker_absence):
                return
        self._drive(disk_id, Detach())

    def _drive(self, disk_id: str, goal: VolumeGoal) -> DiskVolumeSnapshot:
        for _ in range(_MAX_STEPS):
            snapshot = self._snapshot(disk_id)
            if isinstance(goal, AttachTo) and snapshot.lease_token != goal.lease_token:
                raise ConflictError(
                    f"disk {disk_id} changed hands while its volume was being attached"
                )
            step = next_step(snapshot, goal)
            if step is None:
                return snapshot
            self._perform(snapshot, step, goal)
        raise UpstreamUnavailableError(f"disk {disk_id} volume did not settle; retry")

    def _perform(self, snapshot: DiskVolumeSnapshot, step: StepKind, goal: VolumeGoal) -> None:
        at = self.clock()
        match step:
            case StepKind.BeginCreate:
                if not isinstance(goal, AttachTo):
                    raise AssertionError("only an attach creates a volume")
                host = goal.host
                size_bytes = goal.size_bytes
                self._record(
                    lambda volumes: volumes.transition(
                        snapshot,
                        at=at,
                        state=VolumeState.Creating,
                        provider_ref=host.provider_ref,
                        connection_id=host.connection_id,
                        region=host.region,
                        zone=host.zone,
                        instance_id=host.instance_id,
                        volume_size_bytes=size_bytes,
                        token=secrets.token_hex(16),
                        formatted=False,
                    )
                )
            case StepKind.Create:
                volume = self._provider(snapshot).create_volume(
                    BlockVolumeRequest(
                        owner=BlockVolumeOwner(
                            deployment=self.deployment,
                            workspace_id=snapshot.workspace_id,
                            disk_id=snapshot.disk_id,
                        ),
                        zone=snapshot.zone,
                        size_bytes=snapshot.volume_size_bytes,
                        throughput_mibps=DISK_VOLUME_THROUGHPUT_MIBPS,
                        token=snapshot.token,
                    )
                )
                self._record(
                    lambda volumes: volumes.transition(
                        snapshot, at=at, state=VolumeState.Attaching, volume_id=volume.volume_id
                    )
                )
            case StepKind.Forget:
                # Whatever that creation made is found by the orphan collection.
                self._move(snapshot, at=at, state=VolumeState.None_)
            case StepKind.Attach:
                try:
                    self._provider(snapshot).attach_volume(
                        snapshot.volume_id, instance_id=snapshot.instance_id
                    )
                except BlockVolumeMissingError:
                    self._move(snapshot, at=at, state=VolumeState.None_)
                    return
                self._move(snapshot, at=at, state=VolumeState.Attached)
            case StepKind.Reattach:
                if not isinstance(goal, AttachTo):
                    raise AssertionError("only an attach takes a volume back")
                host = goal.host
                formatted = snapshot.formatted or snapshot.state == VolumeState.Releasing
                self._record(
                    lambda volumes: volumes.transition(
                        snapshot,
                        at=at,
                        state=VolumeState.Attaching,
                        instance_id=host.instance_id,
                        connection_id=host.connection_id,
                        formatted=formatted,
                    )
                )
            case StepKind.BeginDetach:
                formatted = snapshot.formatted or snapshot.state in {
                    VolumeState.Attached,
                    VolumeState.Releasing,
                }
                self._record(
                    lambda volumes: volumes.transition(
                        snapshot, at=at, state=VolumeState.Detaching, formatted=formatted
                    )
                )
            case StepKind.Detach:
                self._provider(snapshot).detach_volume(
                    snapshot.volume_id, instance_id=snapshot.instance_id
                )
                self._record(
                    lambda volumes: volumes.transition(
                        snapshot, at=at, state=VolumeState.Cached, instance_id=""
                    )
                )
            case StepKind.BeginDelete:
                self._move(snapshot, at=at, state=VolumeState.Deleting)
            case StepKind.Delete:
                self._provider(snapshot).delete_volume(snapshot.volume_id)
                self._move(snapshot, at=at, state=VolumeState.None_)

    def _move(self, snapshot: DiskVolumeSnapshot, *, at: datetime, state: VolumeState) -> None:
        self._record(lambda volumes: volumes.transition(snapshot, at=at, state=state))

    def _record(self, change: Callable[[DiskVolumeRepository], bool]) -> None:
        # A lost race records nothing; the next step reads what the winner wrote.
        with self.database.session() as session:
            change(DiskVolumeRepository(session))

    def _provider(self, snapshot: DiskVolumeSnapshot) -> BlockVolumeProvider:
        return self.providers.volumes(
            BlockVolumeScope(
                workspace_id=snapshot.workspace_id,
                provider_ref=snapshot.provider_ref,
                region=snapshot.region,
            )
        )

    def _snapshot(self, disk_id: str) -> DiskVolumeSnapshot:
        snapshot = self._snapshot_or_none(disk_id)
        if snapshot is None:
            raise NotFoundError(f"disk not found: {disk_id}")
        return snapshot

    def _snapshot_or_none(self, disk_id: str) -> DiskVolumeSnapshot | None:
        with self.database.session() as session:
            return DiskVolumeRepository(session).snapshot(disk_id)


__all__ = [
    "DISK_VOLUME_ORPHAN_INTERVAL_SECONDS",
    "DISK_VOLUME_RELEASE_GRACE_SECONDS",
    "DISK_VOLUME_STUCK_SECONDS",
    "AttachTo",
    "Detach",
    "DiskVolumeGrant",
    "DiskVolumeService",
    "DiskWorkerAbsence",
    "Remove",
    "StepKind",
    "VolumeState",
    "holder_keeps_disk",
    "next_step",
]
