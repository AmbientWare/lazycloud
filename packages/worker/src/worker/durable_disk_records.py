"""Worker payloads for acquiring, publishing, and releasing a durable disk.

The control plane is the only writer of a disk's generation chain. A worker
acquires the disk for one container and receives a fencing token; every publish
and the release carry it, and a token that is no longer current is refused, so a
container that lost the disk cannot publish over the one that holds it.
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


class DiskAcquireResult(ContractModel):
    disk_id: str
    lease_token: str = Field(min_length=1)
    size_bytes: int = Field(gt=0)
    generation: int = Field(ge=0)
    """Newest published generation; 0 means a fresh disk the worker formats."""

    chain: list[DiskChainLayer] = Field(default_factory=list)
    """Layers to restore, base first, ending at ``generation``."""


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

    final: bool = False
    """Release the disk with this publish; the container is stopping."""


class DiskPublishResult(ContractModel):
    generation: int = Field(gt=0)


class DiskCollectPayload(ContractModel):
    """Chunks and manifests a holder deleted after publishing a self-contained layer.

    Only the lease holder collects, right after its parentless generation is
    recorded, so no other writer can be uploading chunks the collection misses.
    """

    container_id: str = Field(min_length=1)
    disk_id: str = Field(min_length=1)
    lease_token: str = Field(min_length=1)
    generation: int = Field(gt=0)
    """The parentless generation every older generation is superseded by."""

    stored_bytes_removed: int = Field(ge=0)


class DiskReleasePayload(ContractModel):
    container_id: str = Field(min_length=1)
    disk_id: str = Field(min_length=1)
    lease_token: str = Field(min_length=1)


__all__ = [
    "DiskAcquirePayload",
    "DiskAcquireResult",
    "DiskChainLayer",
    "DiskCollectPayload",
    "DiskPublishPayload",
    "DiskPublishResult",
    "DiskReleasePayload",
]
