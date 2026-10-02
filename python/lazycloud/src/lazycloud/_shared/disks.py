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

from pydantic import Field, field_validator

from lazycloud.contracts import ContractModel
from lazycloud._shared.resources import parse_memory_mib

DISK_ROOT_MOUNT_PATH = "/"
"""A disk mounted here holds the container's writable root layer.

Everything the container writes outside its volumes then survives a restart:
packages it installs, dotfiles, and its working trees.
"""

DISK_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
MIN_DISK_SIZE_BYTES = 1024**3
DISK_BLOCK_BYTES = 4096


MAX_DISK_SIZE_BYTES = 1024**4


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

    Sizes are whole mebibytes, the same units memory takes. A byte count must
    be whole 4096-byte filesystem blocks.
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
    if size % DISK_BLOCK_BYTES:
        msg = f"disk size must be a multiple of {DISK_BLOCK_BYTES} bytes"
        raise ValueError(msg)
    return size


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


def require_one_writer(max_containers: int) -> None:
    """Refuse more than one container for a workload with a disk; one writes it at a time."""
    if max_containers > 1:
        msg = "a workload with a disk runs one container; set max_containers to 1"
        raise ValueError(msg)


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


__all__ = [
    "DISK_BLOCK_BYTES",
    "DISK_NAME_PATTERN",
    "DISK_ROOT_MOUNT_PATH",
    "MAX_DISK_SIZE_BYTES",
    "MIN_DISK_SIZE_BYTES",
    "DiskMount",
    "parse_disk_size_bytes",
    "require_one_writer",
    "validate_disk_mount_path",
    "validate_disk_mounts",
    "validate_disk_name",
]
