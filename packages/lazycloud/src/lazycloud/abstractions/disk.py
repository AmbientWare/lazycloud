from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from shared.disks import DISK_ROOT_MOUNT_PATH, DiskMount, parse_disk_size_bytes


@dataclass(frozen=True, slots=True)
class Disk:
    """A durable disk, named in the workspace, that a pod keeps across restarts.

    Mounted at ``/`` it holds the pod's whole writable root: installed packages,
    home directories and working trees all survive a stop, a redeploy, or a move
    to another machine. One container writes a disk at a time.
    """

    name: str
    size: str | int = "50Gi"
    mount_path: str = DISK_ROOT_MOUNT_PATH

    def mount(self) -> DiskMount:
        return DiskMount(
            name=self.name,
            size_bytes=parse_disk_size_bytes(self.size),
            mount_path=self.mount_path,
        )


def disk_mounts(disks: Iterable[Disk | DiskMount]) -> list[DiskMount]:
    return [disk.mount() if isinstance(disk, Disk) else disk for disk in disks]


__all__ = ["Disk", "disk_mounts"]
