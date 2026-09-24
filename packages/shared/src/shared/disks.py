"""Durable disks: named block devices a workload keeps across container restarts.

A disk belongs to a workspace by name, the way a volume does, and outlives every
container that mounts it. While a container runs, the disk is a local ext4
filesystem on the node that holds it. Between containers it is a chain of sealed
layers in the workspace bucket, so any node in the workspace's placement can
restore it, and the node that last held it restarts it without a download.

One container writes a disk at a time. Acquiring the disk enforces that with a
fencing token every publish must carry, because two writers of one block device
corrupt it rather than conflict.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import Field, field_validator

from shared.contracts import ContractModel
from shared.deployments import DeploymentKind, PodRole
from shared.enums import StringEnum
from shared.resources import parse_memory_mib
from shared.timestamps import utc_now

DISK_ROOT_MOUNT_PATH = "/"
"""A disk mounted here holds the container's writable root layer.

Everything the container writes outside its volumes then survives a restart:
packages it installs, dotfiles, and its working trees.
"""

DISK_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
MIN_DISK_SIZE_BYTES = 1024**3
DISK_VOLUME_THROUGHPUT_MIBPS = 500
"""Throughput provisioned on each disk's provider volume.

Above gp3's 125 MiB/s baseline so a restore from object storage is bounded by
the network rather than by the volume.
"""

DISK_VOLUME_CACHE_SECONDS = 30 * 60
"""How long the control plane keeps a released disk's volume for a restart in the
same zone before deleting it. The object-storage copy is the durable one."""

DISK_VOLUME_MIN_HEADROOM_BYTES = 10 * 1024**3


def disk_volume_size_bytes(disk_size_bytes: int) -> int:
    """Size of the provider volume that backs a disk of this declared size.

    The volume holds the disk's layer chain: the compacted base, which never
    exceeds the declared size, plus writes not yet sealed, published and
    compacted into it. The headroom covers those, a quarter of the disk or 10 GiB,
    whichever is larger. Flattening streams the chain rather than copying it, so
    no second full copy ever needs room.
    """
    return disk_size_bytes + max(DISK_VOLUME_MIN_HEADROOM_BYTES, disk_size_bytes // 4)


MAX_DISK_SIZE_BYTES = 1024**4
DEFAULT_DISK_FILESYSTEM = "ext4"

DISK_OBJECT_PREFIX = "disks"
"""Workspace-bucket prefix under which each disk keeps its chunks and manifests."""

DISK_USAGE_SUBJECT = "disk"
"""The resource type a disk's usage records and ledger segments are filed under."""

DISK_FLATTEN_DEPTH = 64
"""Published chain length at which the next publish is a parentless full layer.

Keeps a restore to a bounded number of layers. A flattened layer reads and
hashes the whole disk, so this is spaced out: at one publish every two minutes
of writing, a busy disk flattens about every two hours. Chunks are content
addressed, so a flattened layer uploads only what earlier layers did not hold,
and it is the point after which older chunks can be collected.
"""

DISK_PUBLISH_INTERVAL_SECONDS = 120
"""How often a running disk seals and publishes what changed.

Bounds what a node failure loses. An idle disk seals nothing and publishes nothing.
"""

DISK_HOST_RESERVE_BYTES = 20 * 1024**3
"""Space on a node's disk filesystem that no disk may be placed into.

Placement subtracts it from the filesystem to find what disks may reserve, and
an attach refuses to leave less than it free, so the two agree on where a node
is full.
"""


def disk_capacity_bytes(filesystem_bytes: int) -> int:
    """Bytes of declared disk size a filesystem of this size can hold.

    A disk's declared size is the most it can grow to, so placement reserves
    the whole of it against this figure.
    """
    return max(filesystem_bytes - DISK_HOST_RESERVE_BYTES, 0)


def validate_disk_name(value: str) -> str:
    name = value.strip()
    if not DISK_NAME_PATTERN.fullmatch(name):
        msg = (
            "disk name must be 1-63 lowercase letters, digits, or hyphens, "
            "starting and ending with a letter or digit"
        )
        raise ValueError(msg)
    return name


def parse_disk_size_bytes(value: str | int) -> int:
    """Disk size in bytes from a size such as ``"100Gi"`` or a byte count.

    Sizes are whole mebibytes, the same units memory takes.
    """
    if isinstance(value, int):
        size = value
    else:
        mib = parse_memory_mib(value)
        if mib is None:
            msg = "disk size is required"
            raise ValueError(msg)
        size = mib * 1024**2
    if size < MIN_DISK_SIZE_BYTES:
        msg = "disk size must be at least 1Gi"
        raise ValueError(msg)
    if size > MAX_DISK_SIZE_BYTES:
        msg = "disk size must be at most 1Ti"
        raise ValueError(msg)
    return size


def disk_shrink_message(name: str, recorded_size_bytes: int) -> str:
    """Why a declared size below the recorded one is refused, naming the way out."""
    return (
        f"disk {name} is {recorded_size_bytes} bytes and cannot shrink; declare "
        f"{recorded_size_bytes} bytes or more, or delete the disk to start smaller"
    )


def validate_disk_mount_path(value: str) -> str:
    if not value.startswith("/"):
        msg = "disk mount_path must be absolute"
        raise ValueError(msg)
    if value != DISK_ROOT_MOUNT_PATH and value.rstrip("/") != value:
        msg = "disk mount_path must not end with a slash"
        raise ValueError(msg)
    if any(part in {".", ".."} for part in value.split("/")):
        msg = "disk mount_path must not contain . or .. segments"
        raise ValueError(msg)
    return value


class DiskMount(ContractModel):
    """A disk as a workload declares it."""

    name: str
    size_bytes: int = Field(gt=0)
    mount_path: str = DISK_ROOT_MOUNT_PATH

    @field_validator("name")
    @classmethod
    def name_is_valid(cls, value: str) -> str:
        return validate_disk_name(value)

    @field_validator("size_bytes")
    @classmethod
    def size_is_bounded(cls, value: int) -> int:
        return parse_disk_size_bytes(value)

    @field_validator("mount_path")
    @classmethod
    def mount_path_is_valid(cls, value: str) -> str:
        return validate_disk_mount_path(value)

    @property
    def is_root(self) -> bool:
        return self.mount_path == DISK_ROOT_MOUNT_PATH


def validate_disk_mounts(disks: list[DiskMount]) -> list[DiskMount]:
    names = [disk.name for disk in disks]
    if len(names) != len(set(names)):
        msg = "a workload cannot mount the same disk twice"
        raise ValueError(msg)
    paths = [disk.mount_path for disk in disks]
    if len(paths) != len(set(paths)):
        msg = "two disks cannot share a mount_path"
        raise ValueError(msg)
    return disks


class DiskStorage(StringEnum):
    """Where a machine keeps the disks it holds, and so what limits how many."""

    Host = "host"
    """Under one host directory whose space they share; a joined machine."""

    Volume = "volume"
    """Each on a provider volume of its own; limited by the volumes the machine can attach."""


class DiskLayerFormat(StringEnum):
    Qcow2 = "qcow2"
    """A qcow2 file holding what the generation changed over its parent."""

    Raw = "raw"
    """A flattened generation: the disk's whole contents, with zero ranges left out."""


class DiskStatus(StringEnum):
    Detached = "detached"
    Attached = "attached"
    Deleting = "deleting"


class DiskRecord(ContractModel):
    id: str
    name: str
    size_bytes: int = Field(ge=0)
    status: DiskStatus = DiskStatus.Detached
    generation: int = Field(default=0, ge=0)
    """Newest published generation; 0 means nothing was ever published."""

    stored_bytes: int = Field(default=0, ge=0)
    """Bytes the disk's chunks occupy in object storage."""

    holder_container_id: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DiskWorkload(ContractModel):
    """The workload whose container last asked for a disk."""

    app_id: str
    app_name: str
    kind: DeploymentKind = DeploymentKind.Pod
    name: str
    role: PodRole


class DiskLayerChunk(ContractModel):
    """One content-addressed chunk of a sealed layer file.

    Zero regions are not stored: a restore truncates the layer file to its size
    and writes only the listed chunks.
    """

    offset: int = Field(ge=0)
    length: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DiskLayerManifest(ContractModel):
    """What one published generation adds: a qcow2 layer over its parent."""

    disk_id: str
    generation: int = Field(gt=0)
    parent_generation: int = Field(default=0, ge=0)
    """0 when the layer is self-contained, as the first and every flattened one are."""

    virtual_size_bytes: int = Field(gt=0)
    layer_size_bytes: int = Field(ge=0)
    format: DiskLayerFormat = DiskLayerFormat.Qcow2
    filesystem: str = DEFAULT_DISK_FILESYSTEM
    chunks: list[DiskLayerChunk] = Field(default_factory=list)


def disk_object_prefix(disk_id: str) -> str:
    return f"{DISK_OBJECT_PREFIX}/{disk_id}"


def disk_chunk_key(disk_id: str, sha256: str) -> str:
    return f"{disk_object_prefix(disk_id)}/chunks/{sha256[:2]}/{sha256}"


def disk_manifest_key(disk_id: str, generation: int) -> str:
    return f"{disk_object_prefix(disk_id)}/manifests/{generation:012d}.json"


__all__ = [
    "DEFAULT_DISK_FILESYSTEM",
    "DISK_FLATTEN_DEPTH",
    "DISK_HOST_RESERVE_BYTES",
    "DISK_NAME_PATTERN",
    "DISK_OBJECT_PREFIX",
    "DISK_PUBLISH_INTERVAL_SECONDS",
    "DISK_ROOT_MOUNT_PATH",
    "DISK_USAGE_SUBJECT",
    "DISK_VOLUME_CACHE_SECONDS",
    "DISK_VOLUME_MIN_HEADROOM_BYTES",
    "DISK_VOLUME_THROUGHPUT_MIBPS",
    "MAX_DISK_SIZE_BYTES",
    "MIN_DISK_SIZE_BYTES",
    "DiskLayerChunk",
    "DiskLayerFormat",
    "DiskLayerManifest",
    "DiskMount",
    "DiskRecord",
    "DiskStatus",
    "DiskStorage",
    "DiskWorkload",
    "disk_capacity_bytes",
    "disk_chunk_key",
    "disk_manifest_key",
    "disk_object_prefix",
    "disk_shrink_message",
    "disk_volume_size_bytes",
    "parse_disk_size_bytes",
    "validate_disk_mount_path",
    "validate_disk_mounts",
    "validate_disk_name",
]
