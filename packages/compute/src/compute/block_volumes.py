"""Provider block volumes, the devices disks live on while a container holds them.

A provider volume is created in one zone and attaches to one machine in that
zone at a time. Every operation here is idempotent against the provider's own
record, because the control plane that drives a volume can crash between any
two calls and the next caller has only the durable disk row to go on.

A snapshot copies one volume whole and belongs to the region, so a volume made
from it may be in any zone there. The provider loads its blocks as they are
first read, which is what lets a large disk start without a download.

Ownership travels in the volume's tags. Cleanup never touches a volume this
platform did not tag, or tagged for another deployment. The listing returns only
volumes carrying the deployment's own tags, and a caller deciding what to delete
checks the owner again rather than trusting the filter.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import ConfigDict, Field
from shared.contracts import ContractModel


class BlockVolumeState(StrEnum):
    Creating = "creating"
    Available = "available"
    InUse = "in-use"
    Deleting = "deleting"
    Deleted = "deleted"
    Error = "error"


class BlockVolumeOwner(ContractModel):
    """Who a volume belongs to, as its tags state it."""

    model_config = ContractModel.model_config | ConfigDict(frozen=True)

    deployment: str = Field(min_length=1, max_length=128)
    """The platform deployment's namespace; two deployments never collect each other's volumes."""

    workspace_id: str = Field(min_length=1, max_length=128)
    disk_id: str = Field(min_length=1, max_length=128)


class BlockVolumeRequest(ContractModel):
    owner: BlockVolumeOwner
    zone: str = Field(min_length=1)
    """The provider's zone identity, as the machine it will attach to records it."""

    size_bytes: int = Field(gt=0)
    throughput_mibps: int = Field(gt=0)
    token: str = Field(min_length=1, max_length=64)
    """Names this one creation: a retry with the same token returns the same volume."""

    snapshot_id: str = ""
    """The snapshot the volume starts as, which the provider loads as blocks are
    first read; empty for a blank volume."""


class BlockVolume(ContractModel):
    volume_id: str = Field(min_length=1)
    zone: str
    size_bytes: int = Field(ge=0)
    state: BlockVolumeState
    attached_instance_id: str = ""
    owner: BlockVolumeOwner | None = None
    """None unless every ownership tag is present."""

    creation_token: str = ""
    """The token of the creation that made it, from its tags; empty when untagged."""

    created_at: datetime | None = None


class BlockSnapshotState(StrEnum):
    Pending = "pending"
    Completed = "completed"
    Error = "error"


class BlockSnapshotRequest(ContractModel):
    owner: BlockVolumeOwner
    volume_id: str = Field(min_length=1)
    generation: int = Field(gt=0)
    """The disk generation the volume holds, recorded in the snapshot's tags."""

    token: str = Field(min_length=1, max_length=64)
    """Names this one creation, so a retry finds the snapshot an earlier try made."""


class BlockSnapshot(ContractModel):
    snapshot_id: str = Field(min_length=1)
    state: BlockSnapshotState
    volume_size_bytes: int = Field(ge=0)
    """The size of the volume it was taken from; a volume made from it is at least this."""

    stored_bytes: int = Field(ge=0)
    """What the provider stores for it: its full size once the provider reports
    one, and its volume's size until then."""

    owner: BlockVolumeOwner | None = None
    """None unless every ownership tag is present."""

    creation_token: str = ""
    created_at: datetime | None = None


class BlockVolumeMissingError(Exception):
    """The provider has no such volume, or it is already being deleted."""


class BlockSnapshotMissingError(Exception):
    """A volume creation named a snapshot the provider no longer has."""


class BlockVolumePendingError(Exception):
    """The provider accepted the operation but had not finished it within the wait.

    Every operation is idempotent, so calling it again later picks up where this
    one left off rather than starting another.
    """


class BlockVolumeProvider(Protocol):
    """Block volumes in one provider account and region."""

    def create_volume(self, request: BlockVolumeRequest, *, wait_seconds: float) -> BlockVolume:
        """Create and wait until the volume can attach; the same token returns the same one."""
        ...

    def attach_volume(self, volume_id: str, *, instance_id: str, wait_seconds: float) -> None:
        """Attach, wait until attached, and set the volume to delete when the instance terminates.

        Succeeds when already attached there. Raises `BlockVolumeMissingError`
        when the volume is gone. Every wait raises `BlockVolumePendingError` once
        `wait_seconds` pass.
        """
        ...

    def detach_volume(self, volume_id: str, *, instance_id: str, wait_seconds: float) -> None:
        """Detach from this instance and wait until available; a missing volume is detached."""
        ...

    def delete_volume(self, volume_id: str) -> None:
        """Delete a detached volume; a missing one is already deleted."""
        ...

    def describe_volumes(self, *, deployment: str) -> tuple[BlockVolume, ...]:
        """Every disk volume this deployment tagged in this account and region."""
        ...

    def create_snapshot(self, request: BlockSnapshotRequest) -> BlockSnapshot:
        """Start a snapshot of the volume as it is now, without waiting for it to finish.

        A snapshot already carrying the request's token is returned instead of
        starting another. Raises `BlockVolumePendingError` when the provider
        refuses another snapshot of the volume this soon, and
        `BlockVolumeMissingError` when the volume is gone.
        """
        ...

    def find_snapshot(self, *, token: str) -> BlockSnapshot | None:
        """The snapshot a creation with this token made, if it made one."""
        ...

    def describe_snapshot(self, snapshot_id: str) -> BlockSnapshot | None:
        """The snapshot as the provider has it now; None once it is gone."""
        ...

    def delete_snapshot(self, snapshot_id: str) -> None:
        """Delete a snapshot; a missing one is already deleted.

        Raises `BlockVolumePendingError` while the provider cannot delete it yet.
        """
        ...

    def describe_snapshots(self, *, deployment: str) -> tuple[BlockSnapshot, ...]:
        """Every disk snapshot this deployment tagged in this account and region."""
        ...


@dataclass(frozen=True, slots=True)
class BlockVolumeScope:
    """Where one disk's volume lives: the provider that owns the machine, and its region."""

    workspace_id: str
    provider_ref: str
    region: str


class BlockVolumeProviders(Protocol):
    def volumes(self, scope: BlockVolumeScope) -> BlockVolumeProvider: ...


def orphaned_volumes(
    volumes: tuple[BlockVolume, ...],
    *,
    deployment: str,
    recorded: Mapping[str, str],
    creating: frozenset[str] = frozenset(),
) -> tuple[BlockVolume, ...]:
    """The volumes of this deployment that no disk records and nothing holds.

    `recorded` maps each disk id that still has a row to the volume its row
    names, empty when it names none; `creating` holds the disks whose row is
    part way through creating a volume it has not recorded yet. A volume is an
    orphan when its disk has no row, or when the row names a different volume
    and is not creating one. An attached volume is never an orphan. It is some
    machine's live device, and its disk's cleanup handles whatever left it
    attached. A volume without this deployment's complete ownership tags is never
    an orphan either, whatever the provider's listing returned.
    """
    orphans: list[BlockVolume] = []
    for volume in volumes:
        owner = volume.owner
        if owner is None or owner.deployment != deployment:
            continue
        if volume.attached_instance_id or volume.state is not BlockVolumeState.Available:
            continue
        if owner.disk_id in creating:
            continue
        if recorded.get(owner.disk_id) == volume.volume_id:
            continue
        orphans.append(volume)
    return tuple(orphans)


def orphaned_snapshots(
    snapshots: tuple[BlockSnapshot, ...],
    *,
    deployment: str,
    recorded_ids: frozenset[str],
    recorded_tokens: frozenset[str],
) -> tuple[BlockSnapshot, ...]:
    """The snapshots of this deployment that no disk snapshot row records.

    A row is written before its snapshot is created, so a snapshot whose id and
    creation token are both unrecorded belongs to no disk. One still pending is
    left for a later pass, and one without this deployment's complete ownership
    tags is never an orphan.
    """
    return tuple(
        snapshot
        for snapshot in snapshots
        if snapshot.owner is not None
        and snapshot.owner.deployment == deployment
        and snapshot.state is not BlockSnapshotState.Pending
        and snapshot.snapshot_id not in recorded_ids
        and snapshot.creation_token not in recorded_tokens
    )


__all__ = [
    "BlockSnapshot",
    "BlockSnapshotMissingError",
    "BlockSnapshotRequest",
    "BlockSnapshotState",
    "BlockVolume",
    "BlockVolumeMissingError",
    "BlockVolumeOwner",
    "BlockVolumePendingError",
    "BlockVolumeProvider",
    "BlockVolumeProviders",
    "BlockVolumeRequest",
    "BlockVolumeScope",
    "BlockVolumeState",
    "orphaned_snapshots",
    "orphaned_volumes",
]
