"""Worker payloads for acquiring, publishing, and releasing a durable disk.

The control plane is the only writer of a disk's generation chain. A worker
acquires the disk for one container and receives a fencing token. Every publish
and the release carry it. The control plane refuses a token that is no longer
current, so a container that lost the disk cannot publish over the one that holds it.
"""

from __future__ import annotations

from pydantic import Field
from shared.contracts import ContractModel


class DiskChainLayer(ContractModel):
    """One published generation a restore materializes, base first."""

    generation: int = Field(gt=0)
    manifest_key: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DiskAcquirePayload(ContractModel):
    container_id: str = Field(min_length=1)
    disk_id: str = Field(min_length=1)


class DiskBlockVolume(ContractModel):
    """The provider volume attached to this worker's machine for one disk.

    The worker finds the device by the volume's identifier, formats it on first
    use, and keeps the disk's local layers on it. The object-storage chain stays
    the durable copy; the volume is a cache that the control plane deletes a
    while after the disk is released.
    """

    volume_id: str = Field(min_length=1)
    formatted: bool
    """Whether an earlier lease held this volume. Only a hint. The worker probes
    the device either way and formats one that holds no filesystem."""


class DiskAcquireResult(ContractModel):
    disk_id: str
    lease_token: str = Field(min_length=1)
    size_bytes: int = Field(gt=0)
    generation: int = Field(ge=0)
    """Newest published generation; 0 means a fresh disk the worker formats."""

    chain: list[DiskChainLayer] = Field(default_factory=list)
    """Layers to restore, base first, ending at ``generation``."""

    volume: DiskBlockVolume | None = None
    """Set on provider machines; absent on joined machines, whose disks use host storage."""


class DiskPublishPayload(ContractModel):
    container_id: str = Field(min_length=1)
    disk_id: str = Field(min_length=1)
    lease_token: str = Field(min_length=1)
    generation: int = Field(gt=0)
    """Must be exactly one past the disk's current generation."""

    parent_generation: int = Field(ge=0)
    """0 for a self-contained layer; otherwise the generation it builds on."""

    manifest_key: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stored_bytes_added: int = Field(ge=0)
    """Bytes of chunks this publish uploaded that the disk did not already store."""


class DiskPublishResult(ContractModel):
    generation: int = Field(gt=0)


class DiskCollectPayload(ContractModel):
    """Chunks and manifests a holder deleted after publishing a self-contained layer.

    Only the lease holder collects, right after the control plane records its
    parentless generation, so no other writer can be uploading chunks the
    collection misses.
    """

    container_id: str = Field(min_length=1)
    disk_id: str = Field(min_length=1)
    lease_token: str = Field(min_length=1)
    generation: int = Field(gt=0)
    """The parentless generation that supersedes every older generation."""

    stored_bytes_removed: int = Field(ge=0)


class DiskReleasePayload(ContractModel):
    container_id: str = Field(min_length=1)
    disk_id: str = Field(min_length=1)
    lease_token: str = Field(min_length=1)


class DiskStorageRequest(ContractModel):
    """Workspace storage for the disk's engine, on the authority of its lease.

    The lease outlives the container's scheduler state: a release after a stop
    still has to publish the disk's last generation, which needs the bucket.
    """

    container_id: str = Field(min_length=1)
    disk_id: str = Field(min_length=1)
    lease_token: str = Field(min_length=1)


__all__ = [
    "DiskAcquirePayload",
    "DiskAcquireResult",
    "DiskBlockVolume",
    "DiskChainLayer",
    "DiskCollectPayload",
    "DiskPublishPayload",
    "DiskPublishResult",
    "DiskReleasePayload",
    "DiskStorageRequest",
]
